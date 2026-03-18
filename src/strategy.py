"""
末期定价套利策略引擎（与 scripts/collect_data.py 的 analyze_opportunity 保持同步）

三条信号路径：
  1. 末期套利：gap >= 0.05%  + minute >= 3  → 理论胜率查表
  2. 跟赔率  ：gap >= 0.05%  + minute >= 3  + gap 与强势赔率反向 → 赔率主导
  3. 赔率强信号：gap < 0.05% + 赔率单边 >= 0.72 + (已跳变+min>=1 或 min>=3) → 直接跟市场定价
"""

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .config import cfg
from .data_feed import MarketInfo, OrderBook

logger = logging.getLogger("strategy")

CL_FRESH_SECS      = 45
BN_COUNTER_THRESH  = 0.08
STRONG_ODDS_THRESH = 0.72


class Direction(str, Enum):
    UP   = "Up"
    DOWN = "Down"


@dataclass
class Signal:
    direction: Direction
    token_id:  str
    theoretical_win_rate: float
    market_price: float
    ev_per_unit: float
    ev_after_fee: float
    gap_pct: float
    gap_src: str
    seconds_remaining: int
    kelly_fraction: float
    bet_amount: float
    signal_type: str
    note: str = ""

    @property
    def is_valid(self) -> bool:
        return (
            self.ev_per_unit >= cfg.min_ev_threshold
            and self.market_price < self.theoretical_win_rate
            and self.kelly_fraction > 0
            and self.bet_amount >= 1.0
        )

    def summary(self) -> str:
        return (
            f"{self.direction.value} | gap={self.gap_pct:+.3f}%({self.gap_src}) | "
            f"TWR={self.theoretical_win_rate:.1%} | mkt={self.market_price:.3f} | "
            f"EV={self.ev_per_unit:+.4f}(费后≈{self.ev_after_fee:+.4f}) | "
            f"bet=${self.bet_amount:.2f} | 剩余{self.seconds_remaining}s"
            + (f" | {self.note}" if self.note else "")
        )


@dataclass
class WindowState:
    window_ts: int
    price_to_beat:   Optional[float] = None
    already_traded:  bool = False
    trade_direction: Optional[Direction] = None
    odds_jumped:     bool = False
    prev_up_odds:    Optional[float] = None


class LateStageArbitrageStrategy:

    def __init__(self, total_capital: float):
        self.total_capital = total_capital
        self._window_states: dict[int, WindowState] = {}

    def get_window_state(self, window_ts: int) -> WindowState:
        if window_ts not in self._window_states:
            self._window_states[window_ts] = WindowState(window_ts=window_ts)
            old_keys = sorted(self._window_states.keys())[:-10]
            for k in old_keys:
                del self._window_states[k]
        return self._window_states[window_ts]

    def set_price_to_beat(self, window_ts: int, price: float):
        state = self.get_window_state(window_ts)
        if state.price_to_beat is None:
            state.price_to_beat = price
            logger.info("窗口 %d 开盘价（PtB）= $%.2f", window_ts, price)

    def update_odds_history(self, window_ts: int, up_odds: float):
        state = self.get_window_state(window_ts)
        if state.prev_up_odds is not None:
            delta = up_odds - state.prev_up_odds
            if abs(delta) >= 0.10:
                state.odds_jumped = True
                jump_dir = "UP" if delta > 0 else "DOWN"
                logger.info("🔔 赔率跳变 %+.2f → %s=%.2f (CLOB实时)",
                            delta, jump_dir, up_odds if delta > 0 else (1 - up_odds))
        state.prev_up_odds = up_odds

    def evaluate(
        self,
        window_ts: int,
        btc_price: float,
        market: MarketInfo,
        orderbook: Optional[OrderBook] = None,
        seconds_elapsed: Optional[int] = None,
        cl_gap: Optional[float] = None,
        cl_age: Optional[int]   = None,
        bn_gap: Optional[float] = None,
        ptb_delay_secs: int = 0,
    ) -> Optional[Signal]:
        state = self.get_window_state(window_ts)

        if state.price_to_beat is None:
            return None

        elapsed        = seconds_elapsed if seconds_elapsed is not None else (int(time.time()) - window_ts)
        secs_remaining = 300 - elapsed
        minute         = elapsed // 60

        if state.already_traded:
            return None
        if market.closed or not market.active:
            return None
        if ptb_delay_secs > 60:
            return None

        # ── 选择最可靠的 gap ──
        cl_fresh = (cl_age is not None and cl_age < CL_FRESH_SECS)
        if cl_fresh and cl_gap is not None:
            gap     = cl_gap
            gap_src = "CL链上"
        else:
            gap     = (btc_price - state.price_to_beat) / state.price_to_beat * 100
            gap_src = "BN"

        up_odds   = market.up_odds
        down_odds = market.down_odds
        _bn_gap   = bn_gap if bn_gap is not None else gap

        # ── 路径 0：软冲突检查 ──
        if abs(gap) >= 0.05 and minute >= 2:
            signal_up = gap > 0
            opp_odds  = up_odds if not signal_up else down_odds
            soft_thr  = 0.58 if state.odds_jumped else 0.65
            if opp_odds > soft_thr:
                logger.info("⚠️ 软冲突: gap→%s 但对立赔率=%.2f>%.2f",
                            "UP" if signal_up else "DOWN", opp_odds, soft_thr)
                return None

        # ── 路径 1（跟赔率）：gap >= 0.05 且赔率方向与 gap 相反 ──
        if abs(gap) >= 0.05 and minute >= 3:
            gap_up     = gap > 0
            mkt_up_dom = up_odds   > 0.60
            mkt_dn_dom = down_odds > 0.60
            if (gap_up and mkt_dn_dom) or (not gap_up and mkt_up_dom):
                mkt_dir   = "UP"   if mkt_up_dom else "DOWN"
                mkt_price = up_odds if mkt_up_dom else down_odds

                bn_counter = (
                    (mkt_dir == "DOWN" and _bn_gap > BN_COUNTER_THRESH) or
                    (mkt_dir == "UP"   and _bn_gap < -BN_COUNTER_THRESH)
                )
                if bn_counter:
                    logger.info("⚠️ 赔率存疑: BN反向确认 %s", mkt_dir)
                    return None
                if mkt_price < STRONG_ODDS_THRESH:
                    logger.debug("赔率不够强: %s=%.2f < %.2f", mkt_dir, mkt_price, STRONG_ODDS_THRESH)
                    return None

                theo_wr  = 0.897 if mkt_price < 0.85 else 0.968
                ev       = theo_wr * (1 - mkt_price) - (1 - theo_wr) * mkt_price
                fee_frac = 0.25 * (mkt_price * (1 - mkt_price)) ** 2
                ev_fee   = ev - theo_wr * fee_frac
                if ev > cfg.min_ev_threshold:
                    direction = Direction.UP if mkt_up_dom else Direction.DOWN
                    token_id  = market.up_token if mkt_up_dom else market.down_token
                    kelly     = self._kelly(theo_wr, mkt_price)
                    return Signal(
                        direction=direction, token_id=token_id,
                        theoretical_win_rate=theo_wr, market_price=mkt_price,
                        ev_per_unit=ev, ev_after_fee=ev_fee, gap_pct=gap, gap_src=gap_src,
                        seconds_remaining=secs_remaining, kelly_fraction=kelly,
                        bet_amount=self.total_capital * kelly, signal_type="跟赔率",
                        note=f"赔率{mkt_dir}={mkt_price:.2f}（gap反向以赔率为准）",
                    )
                return None

        # ── 路径 2（赔率强信号）：gap 不足但赔率极强 ──
        # 适用：CL gap < 0.05% 但 CLOB 赔率已明确指向一侧
        # 触发：赔率单边 >= 0.72 + (已跳变+min>=1) 或 min>=3
        sig = self._eval_odds_only(state, market, gap, gap_src, minute, secs_remaining)
        if sig is not None:
            return sig

        # ── 路径 3（末期套利）：gap >= 0.05% ──
        gap_abs = abs(gap)
        theo_wr = self._theo_win_rate(gap_abs, minute)
        if theo_wr < 0.85:
            return None

        direction    = Direction.UP if gap > 0 else Direction.DOWN
        market_price = up_odds if gap > 0 else down_odds
        token_id     = market.up_token if gap > 0 else market.down_token

        edge = theo_wr - market_price
        if edge < cfg.entry_margin:
            return None

        ev       = theo_wr * (1 - market_price) - (1 - theo_wr) * market_price
        fee_frac = 0.25 * (market_price * (1 - market_price)) ** 2
        ev_fee   = ev - theo_wr * fee_frac

        if ev < cfg.min_ev_threshold:
            return None

        if orderbook is not None:
            theo_wr = self._adjust_for_orderbook(theo_wr, direction, market_price, orderbook)

        kelly    = self._kelly(theo_wr, market_price)
        sig_type = "末期套利" if gap_abs >= 0.10 else "弱套利"
        signal   = Signal(
            direction=direction, token_id=token_id,
            theoretical_win_rate=theo_wr, market_price=market_price,
            ev_per_unit=ev, ev_after_fee=ev_fee, gap_pct=gap, gap_src=gap_src,
            seconds_remaining=secs_remaining, kelly_fraction=kelly,
            bet_amount=self.total_capital * kelly, signal_type=sig_type,
        )
        if signal.is_valid:
            logger.info("🟢 %s 信号: %s", sig_type, signal.summary())
        return signal if signal.is_valid else None

    # ── 辅助方法 ──────────────────────────────────────────────

    def _eval_odds_only(
        self, state: WindowState, market: MarketInfo,
        gap: float, gap_src: str, minute: int, secs_remaining: int,
    ) -> Optional[Signal]:
        """
        路径2：gap 不足（< 0.05%）但 CLOB 赔率极强时的独立信号。
        触发条件：赔率单边 >= 0.72 且（已跳变+minute>=1）或（minute>=3）
        """
        odds_dominant = max(market.up_odds, market.down_odds)
        if odds_dominant < STRONG_ODDS_THRESH:
            return None

        odds_dir  = Direction.DOWN if market.down_odds >= market.up_odds else Direction.UP
        odds_px   = market.down_odds if odds_dir == Direction.DOWN else market.up_odds
        token_id  = market.down_token if odds_dir == Direction.DOWN else market.up_token

        timing_ok     = (state.odds_jumped and minute >= 1) or minute >= 3
        gap_conflicts = (
            abs(gap) >= 0.05 and
            ((odds_dir == Direction.DOWN and gap > 0) or
             (odds_dir == Direction.UP   and gap < 0))
        )
        if not timing_ok or gap_conflicts:
            return None

        theo_wr  = 0.897 if odds_px < 0.85 else 0.968
        ev       = theo_wr * (1 - odds_px) - (1 - theo_wr) * odds_px
        fee_frac = 0.25 * (odds_px * (1 - odds_px)) ** 2
        ev_fee   = ev - theo_wr * fee_frac

        if ev < cfg.min_ev_threshold:
            return None

        kelly     = self._kelly(theo_wr, odds_px)
        jump_note = "跳变+" if state.odds_jumped else ""
        sig = Signal(
            direction=odds_dir, token_id=token_id,
            theoretical_win_rate=theo_wr, market_price=odds_px,
            ev_per_unit=ev, ev_after_fee=ev_fee, gap_pct=gap, gap_src=gap_src,
            seconds_remaining=secs_remaining, kelly_fraction=kelly,
            bet_amount=self.total_capital * kelly, signal_type="跟赔率",
            note=f"{jump_note}赔率={odds_px:.2f}（gap不足，以市场定价为准）",
        )
        if sig.is_valid:
            logger.info("🟢 赔率强信号: %s", sig.summary())
        return sig if sig.is_valid else None

    def mark_traded(self, window_ts: int, direction: Direction):
        state = self.get_window_state(window_ts)
        state.already_traded   = True
        state.trade_direction  = direction

    def update_capital(self, new_capital: float):
        self.total_capital = new_capital

    @staticmethod
    def _theo_win_rate(gap_abs: float, minute: int) -> float:
        if minute < 3:
            return 0.5
        if gap_abs >= 0.30: return 0.995
        if gap_abs >= 0.20: return 0.982
        if gap_abs >= 0.15: return 0.979
        if gap_abs >= 0.10: return 0.968
        if gap_abs >= 0.05: return 0.897
        return 0.5

    @staticmethod
    def _kelly(win_rate: float, entry_price: float) -> float:
        b = (1 - entry_price) / entry_price
        if b <= 0:
            return 0.0
        kelly = (b * win_rate - (1 - win_rate)) / b
        return max(0.0, min(kelly * 0.5, cfg.max_bet_fraction))

    @staticmethod
    def _adjust_for_orderbook(
        theo_wr: float, direction: Direction,
        market_price: float, ob: OrderBook,
    ) -> float:
        depth = ob.ask_depth_at(market_price + 0.02)
        if depth < 10:
            return theo_wr * 0.98
        return theo_wr


# ─────────────────────────────────────────────
# 风险控制器
# ─────────────────────────────────────────────

class RiskManager:
    def __init__(self, initial_capital: float):
        self.initial_capital     = initial_capital
        self.current_capital     = initial_capital
        self._day_start_capital  = initial_capital
        self._day_start_ts       = self._today_ts()
        self._consecutive_losses = 0
        self._paused_until: float = 0.0
        self._total_trades = 0
        self._total_wins   = 0

    def allow_trade(self) -> tuple[bool, str]:
        now = time.time()
        if now < self._paused_until:
            return False, f"连续亏损熔断，剩余暂停 {int(self._paused_until - now)}s"
        self._refresh_day()
        day_loss = (self._day_start_capital - self.current_capital) / self._day_start_capital
        if day_loss >= cfg.max_daily_loss_fraction:
            return False, f"日亏损 {day_loss:.1%} 已达上限"
        if self.current_capital < self.initial_capital * 0.10:
            return False, "资金不足初始的 10%"
        return True, "ok"

    def record_result(self, win: bool, pnl: float):
        self.current_capital += pnl
        self._total_trades   += 1
        if win:
            self._total_wins         += 1
            self._consecutive_losses  = 0
        else:
            self._consecutive_losses += 1
            if self._consecutive_losses >= cfg.max_consecutive_losses:
                self._paused_until = time.time() + cfg.pause_after_loss_minutes * 60
                logger.warning("连续亏损 %d 次！暂停 %d 分钟",
                               self._consecutive_losses, cfg.pause_after_loss_minutes)

    @property
    def win_rate(self) -> float:
        return self._total_wins / self._total_trades if self._total_trades > 0 else 0.0

    @property
    def stats(self) -> dict:
        pnl = self.current_capital - self.initial_capital
        return {
            "capital":   self.current_capital,
            "pnl":       pnl,
            "pnl_pct":   pnl / self.initial_capital,
            "trades":    self._total_trades,
            "wins":      self._total_wins,
            "win_rate":  self.win_rate,
            "cons_loss": self._consecutive_losses,
        }

    def _refresh_day(self):
        today = self._today_ts()
        if today > self._day_start_ts:
            self._day_start_capital = self.current_capital
            self._day_start_ts      = today

    @staticmethod
    def _today_ts() -> int:
        now = int(time.time())
        return now - (now % 86400)
