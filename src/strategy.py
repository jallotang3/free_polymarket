"""
末期定价套利策略引擎

核心逻辑：
  在 5 分钟窗口的第 3:30~4:30 分钟，当 BTC 价格已明显领先开盘价时，
  比较「理论胜率」与 Polymarket 实时赔率的差距，若差距 > 安全边际则入场。

回测依据（7天/2015窗口）：
  第4分钟 gap ≥ 0.10% → 真实胜率 96.8%
  第4分钟 gap ≥ 0.20% → 真实胜率 98.2%
"""

import logging
import math
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .config import cfg
from .data_feed import MarketInfo, OrderBook

logger = logging.getLogger("strategy")


class Direction(str, Enum):
    UP   = "Up"
    DOWN = "Down"


@dataclass
class Signal:
    direction: Direction
    token_id: str
    theoretical_win_rate: float   # 基于历史数据的理论胜率
    market_price: float           # Polymarket 当前赔率（我们要买入的价格）
    ev_per_unit: float            # 期望收益/单位（正数才操作）
    gap_pct: float                # BTC 价格与开盘价的差距百分比
    seconds_remaining: int        # 窗口剩余秒数
    kelly_fraction: float         # Kelly 建议仓位比例
    bet_amount: float             # 建议投入金额（= 总资金 × kelly_fraction）

    @property
    def is_valid(self) -> bool:
        return (
            self.ev_per_unit >= cfg.min_ev_threshold
            and self.market_price < self.theoretical_win_rate
            and self.kelly_fraction > 0
            and self.bet_amount > 1.0  # 至少 $1
        )

    def summary(self) -> str:
        return (
            f"{self.direction.value} | gap={self.gap_pct:+.3f}% | "
            f"TWR={self.theoretical_win_rate:.1%} | mkt={self.market_price:.3f} | "
            f"EV={self.ev_per_unit:+.4f} | bet=${self.bet_amount:.2f} | "
            f"剩余{self.seconds_remaining}s"
        )


@dataclass
class WindowState:
    """记录一个 5 分钟窗口的状态"""
    window_ts: int
    price_to_beat: Optional[float] = None   # 开盘参考价
    already_traded: bool = False            # 本窗口是否已下单
    trade_direction: Optional[Direction] = None


class LateStageArbitrageStrategy:
    """
    末期定价套利策略

    关键参数（来自 config.py）：
      min_gap_pct:       触发信号的最小价格差距（默认 0.10%）
      entry_margin:      理论胜率 - 市场赔率 的最小安全边际（默认 0.03）
      min_ev_threshold:  最低期望收益（默认 0.02/单位）
      entry_window_start/end: 允许入场的时间窗口（默认 210~270 秒，即 3:30~4:30）
    """

    def __init__(self, total_capital: float):
        self.total_capital = total_capital
        self._window_states: dict[int, WindowState] = {}

    def get_window_state(self, window_ts: int) -> WindowState:
        if window_ts not in self._window_states:
            self._window_states[window_ts] = WindowState(window_ts=window_ts)
            # 清理旧窗口（只保留最近10个）
            old_keys = sorted(self._window_states.keys())[:-10]
            for k in old_keys:
                del self._window_states[k]
        return self._window_states[window_ts]

    def set_price_to_beat(self, window_ts: int, price: float):
        """在窗口开盘时记录 Price-to-Beat"""
        state = self.get_window_state(window_ts)
        if state.price_to_beat is None:
            state.price_to_beat = price
            logger.info("窗口 %d 开盘价（Price-to-Beat）= $%.2f", window_ts, price)

    def evaluate(
        self,
        window_ts: int,
        btc_price: float,
        market: MarketInfo,
        orderbook: Optional[OrderBook] = None,
        seconds_elapsed: Optional[int] = None,
    ) -> Optional[Signal]:
        """
        评估当前是否有套利信号。
        返回 Signal（含 is_valid 字段）或 None（数据不足）。
        """
        state = self.get_window_state(window_ts)

        # 1. 确保有开盘价
        if state.price_to_beat is None:
            logger.debug("窗口 %d 无开盘价，跳过", window_ts)
            return None

        # 2. 时间检查：只在入场窗口内操作
        elapsed = seconds_elapsed if seconds_elapsed is not None else (int(time.time()) - window_ts)
        secs_remaining = 300 - elapsed
        if not (cfg.entry_window_start <= elapsed <= cfg.entry_window_end):
            return None

        # 3. 本窗口已下单，不重复操作
        if state.already_traded:
            logger.debug("窗口 %d 本轮已下单(%s)，跳过重复评估", window_ts, state.trade_direction)
            return None

        # 4. 市场状态检查
        if market.closed or not market.active:
            logger.warning("窗口 %d 市场状态异常: closed=%s active=%s", window_ts, market.closed, market.active)
            return None

        # 5. 计算 gap
        gap_pct = (btc_price - state.price_to_beat) / state.price_to_beat * 100

        if abs(gap_pct) < cfg.min_gap_pct:
            logger.debug(
                "窗口 %d gap 不足: |%.4f%%| < %.2f%%  BTC=$%.2f PtB=$%.2f  elapsed=%ds",
                window_ts, gap_pct, cfg.min_gap_pct, btc_price, state.price_to_beat, elapsed,
            )
            return None

        # 6. 方向和赔率
        if gap_pct > 0:
            direction = Direction.UP
            market_price = market.up_odds
            token_id = market.up_token
            opposite_odds = market.down_odds
        else:
            direction = Direction.DOWN
            market_price = market.down_odds
            token_id = market.down_token
            opposite_odds = market.up_odds

        # ── 关键验证：方向一致性检查 ──
        # 问题根源：我们用 Binance PtB，Polymarket 用 Chainlink PtB，两者可能不同。
        # 若市场赔率显示"对立方向"的概率 > 0.60，说明 Chainlink 判断与我们相反，
        # 此时绝对不能下注（如 15:34 BTC Binance下跌但市场 Up=0.86 的情形）。
        if opposite_odds > 0.60:
            logger.warning(
                "⛔ 方向冲突！Binance信号=%s(gap=%+.3f%%) 但市场认为对立方向概率=%.2f "
                "——Chainlink与Binance价格可能存在偏差，跳过",
                direction.value, gap_pct, opposite_odds,
            )
            return None

        # 7. 理论胜率
        minute_in_window = elapsed // 60
        theo_wr = cfg.theoretical_win_rate(abs(gap_pct), minute_in_window)

        # 动态调整：根据订单簿深度修正胜率（可选）
        if orderbook is not None:
            theo_wr = self._adjust_for_orderbook(theo_wr, direction, market_price, orderbook)

        # 8. 安全边际检查
        edge = theo_wr - market_price
        if edge < cfg.entry_margin:
            logger.info(
                "⚪ 边际不足 窗口%d | %s gap=%+.3f%% TWR=%.1f%% mkt=%.3f edge=%.4f < %.2f",
                window_ts, direction.value, gap_pct, theo_wr * 100, market_price, edge, cfg.entry_margin,
            )
            return None

        # 9. 期望收益
        ev = theo_wr * (1 - market_price) - (1 - theo_wr) * market_price

        # 10. Kelly 仓位
        kelly_frac = self._kelly(theo_wr, market_price)
        bet = self.total_capital * kelly_frac

        signal = Signal(
            direction=direction,
            token_id=token_id,
            theoretical_win_rate=theo_wr,
            market_price=market_price,
            ev_per_unit=ev,
            gap_pct=gap_pct,
            seconds_remaining=secs_remaining,
            kelly_fraction=kelly_frac,
            bet_amount=bet,
        )

        if signal.is_valid:
            logger.info("🟢 套利信号: %s", signal.summary())
        else:
            logger.debug("信号无效 (EV=%.4f): %s", ev, signal.summary())

        return signal

    def mark_traded(self, window_ts: int, direction: Direction):
        state = self.get_window_state(window_ts)
        state.already_traded = True
        state.trade_direction = direction

    def update_capital(self, new_capital: float):
        self.total_capital = new_capital

    # ── 内部计算 ──

    @staticmethod
    def _kelly(win_rate: float, entry_price: float) -> float:
        """
        Kelly 公式（半 Kelly，上限 max_bet_fraction）
        b = (1 - entry) / entry  （每单位投入的净盈利倍数）
        Kelly = (b * p - q) / b
        """
        b = (1 - entry_price) / entry_price
        if b <= 0:
            return 0.0
        q = 1 - win_rate
        kelly = (b * win_rate - q) / b
        half_kelly = kelly * 0.5
        return max(0.0, min(half_kelly, cfg.max_bet_fraction))

    @staticmethod
    def _adjust_for_orderbook(
        theo_wr: float,
        direction: Direction,
        market_price: float,
        ob: OrderBook,
    ) -> float:
        """
        根据订单簿深度微调理论胜率：
        - 如果 asks 在 market_price 附近几乎没有流动性，可能需要更高的价格成交，降低吸引力
        - 如果深度充足，维持原理论胜率
        """
        depth = ob.ask_depth_at(market_price + 0.02)
        if depth < 10:  # 流动性不足 $10
            logger.debug("订单簿深度不足 (%.1f)，下调理论胜率", depth)
            return theo_wr * 0.98
        return theo_wr


# ─────────────────────────────────────────────
# 风险控制器
# ─────────────────────────────────────────────

class RiskManager:
    """
    全局风险控制，独立于策略逻辑之外。
    负责：日亏损限额、连续亏损熔断、资金更新。
    """

    def __init__(self, initial_capital: float):
        self.initial_capital = initial_capital
        self.current_capital = initial_capital
        self._day_start_capital = initial_capital
        self._day_start_ts = self._today_ts()
        self._consecutive_losses = 0
        self._paused_until: float = 0.0
        self._total_trades = 0
        self._total_wins = 0

    def allow_trade(self) -> tuple[bool, str]:
        """
        返回 (是否允许交易, 原因)
        """
        now = time.time()

        # 暂停检查
        if now < self._paused_until:
            remaining = int(self._paused_until - now)
            return False, f"连续亏损熔断，剩余暂停 {remaining}s"

        # 日亏损限额
        self._refresh_day()
        day_loss = (self._day_start_capital - self.current_capital) / self._day_start_capital
        if day_loss >= cfg.max_daily_loss_fraction:
            return False, f"日亏损 {day_loss:.1%} 已达上限 {cfg.max_daily_loss_fraction:.0%}"

        # 资金最低保护（不到初始资金 10%）
        if self.current_capital < self.initial_capital * 0.10:
            return False, "资金不足初始的 10%，停止交易"

        return True, "ok"

    def record_result(self, win: bool, pnl: float):
        self.current_capital += pnl
        self._total_trades += 1
        if win:
            self._total_wins += 1
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
            if self._consecutive_losses >= cfg.max_consecutive_losses:
                pause_secs = cfg.pause_after_loss_minutes * 60
                self._paused_until = time.time() + pause_secs
                logger.warning(
                    "连续亏损 %d 次！暂停 %d 分钟",
                    self._consecutive_losses, cfg.pause_after_loss_minutes
                )

    @property
    def win_rate(self) -> float:
        return self._total_wins / self._total_trades if self._total_trades > 0 else 0.0

    @property
    def stats(self) -> dict:
        pnl = self.current_capital - self.initial_capital
        return {
            "capital":    self.current_capital,
            "pnl":        pnl,
            "pnl_pct":    pnl / self.initial_capital,
            "trades":     self._total_trades,
            "wins":       self._total_wins,
            "win_rate":   self.win_rate,
            "cons_loss":  self._consecutive_losses,
        }

    def _refresh_day(self):
        today = self._today_ts()
        if today > self._day_start_ts:
            self._day_start_capital = self.current_capital
            self._day_start_ts = today

    @staticmethod
    def _today_ts() -> int:
        now = int(time.time())
        return now - (now % 86400)
