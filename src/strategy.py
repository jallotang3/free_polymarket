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
STRONG_ODDS_THRESH = 0.72  # 路径1（跟赔率）的最低赔率门槛（固定，与贪婪指数无关）
# 路径2：距收盘过短时盘口噪声大（实盘 2026-03-21 #325 剩余31s 巨震后反向）
PATH2_MIN_SECS_REMAINING = 40


class Direction(str, Enum):
    UP   = "Up"
    DOWN = "Down"


@dataclass
class Signal:
    direction: Direction
    token_id:  str
    condition_id: str
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
        # 使用贪婪指数的 min_ev（而非静态 min_ev_threshold），保持一致性
        min_ev = cfg.greed_params["min_ev"]
        return (
            self.ev_per_unit >= min_ev
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
    jump_ts:         float = 0.0    # 最近一次跳变的时间戳（unix）
    jump_dir_up:     Optional[bool] = None  # 跳变方向（True=UP跳, False=DOWN跳）
    jump_count:      int   = 0      # 同向连续跳变次数（用于延长冷却期）


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
                new_dir_up = delta > 0
                # 同向连续跳变计数（用于延长冷却）
                if state.jump_dir_up is not None and state.jump_dir_up == new_dir_up:
                    state.jump_count += 1
                else:
                    state.jump_count = 1
                state.odds_jumped  = True
                state.jump_ts      = time.time()
                state.jump_dir_up  = new_dir_up
                jump_dir = "UP" if delta > 0 else "DOWN"
                logger.info("🔔 赔率跳变 %+.2f → %s=%.2f (连续%d次)",
                            delta, jump_dir,
                            up_odds if delta > 0 else (1 - up_odds),
                            state.jump_count)
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
        signal_confidence: float = 1.0,
        vol_level: str = "medium",  # 新增：波动率等级
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

                # 路径1理论胜率（与852窗口回测一致）
                if mkt_price >= 0.85:
                    theo_wr = 0.960
                elif mkt_price >= 0.78:
                    theo_wr = 0.920
                else:
                    theo_wr = 0.850
                ev       = theo_wr * (1 - mkt_price) - (1 - theo_wr) * mkt_price
                fee_frac = 0.25 * (mkt_price * (1 - mkt_price)) ** 2
                ev_fee   = ev - theo_wr * fee_frac
                if ev > cfg.greed_params["min_ev"]:
                    direction = Direction.UP if mkt_up_dom else Direction.DOWN
                    token_id  = market.up_token if mkt_up_dom else market.down_token
                    kelly     = self._kelly_with_low_odds(theo_wr, mkt_price)
                    return Signal(
                        direction=direction, token_id=token_id,
                        condition_id=market.condition_id,
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
        sig = self._eval_odds_only(state, market, gap, gap_src, minute, secs_remaining, signal_confidence, vol_level)
        if sig is not None:
            return sig

        # ── 路径 3（末期套利）──
        gp_path3 = cfg.greed_params
        gap_abs  = abs(gap)
        min_gap  = gp_path3["min_gap"]
        if gap_abs < max(min_gap, 0.05):  # 最低 0.05% 保底
            return None

        theo_wr = self._theo_win_rate(gap_abs, minute)
        if theo_wr < 0.85:
            return None

        if minute < gp_path3["min_minute"]:
            logger.debug("末期套利分钟拦截: minute=%d (需≥%d, greed=%d)",
                         minute, gp_path3["min_minute"], cfg.greed_index)
            return None

        direction    = Direction.UP if gap > 0 else Direction.DOWN
        market_price = up_odds if gap > 0 else down_odds
        token_id     = market.up_token if gap > 0 else market.down_token

        # 末期套利：高赔率（>0.85）需要更高置信度
        # 实盘案例（05:43 Up@0.885 conf=0.52 震荡 -$5.00）：
        #   conf=0.52 震荡市下的高赔率信号在分3也不可靠，门槛提至0.60
        if market_price > 0.85 and signal_confidence < 0.60:
            logger.debug(
                "末期套利高赔率置信度拦截: price=%.3f conf=%.2f < 0.60",
                market_price, signal_confidence,
            )
            return None

        edge = theo_wr - market_price
        if edge < cfg.entry_margin:
            return None

        ev       = theo_wr * (1 - market_price) - (1 - theo_wr) * market_price
        fee_frac = 0.25 * (market_price * (1 - market_price)) ** 2
        ev_fee   = ev - theo_wr * fee_frac

        if ev < gp_path3["min_ev"]:
            return None

        if orderbook is not None:
            theo_wr = self._adjust_for_orderbook(theo_wr, direction, market_price, orderbook)

        # 低赔率机会允许更高 Kelly 上限
        kelly  = self._kelly_with_low_odds(theo_wr, market_price)
        signal = Signal(
            direction=direction, token_id=token_id,
            condition_id=market.condition_id,
            theoretical_win_rate=theo_wr, market_price=market_price,
            ev_per_unit=ev, ev_after_fee=ev_fee, gap_pct=gap, gap_src=gap_src,
            seconds_remaining=secs_remaining, kelly_fraction=kelly,
            bet_amount=self.total_capital * kelly, signal_type="末期套利",
        )
        if signal.is_valid:
            logger.info("🟢 末期套利信号: %s", signal.summary())
        return signal if signal.is_valid else None

    # ── 辅助方法 ──────────────────────────────────────────────

    def _eval_odds_only(
        self, state: WindowState, market: MarketInfo,
        gap: float, gap_src: str, minute: int, secs_remaining: int,
        signal_confidence: float = 1.0,
        vol_level: str = "medium",  # 新增：波动率等级（从 market_context 传入）
    ) -> Optional[Signal]:
        """
        路径2：赔率强信号，根据贪婪指数动态调整阈值。
        贪婪指数越高，赔率阈值越低（允许更多信号），反之越保守。

        优化（2026-03-23）：
          - 低波动环境提高赔率门槛至0.85
          - 震荡市（conf<0.55）要求赔率≥0.85或gap≥0.10%
          - 分3高赔率gap门槛从0.05提高到0.10
        """
        gp = cfg.greed_params
        min_odds  = gp["min_odds_path2"]
        price_max = gp["price_max"]
        min_min   = gp["min_minute"]

        odds_dominant = max(market.up_odds, market.down_odds)
        if odds_dominant < min_odds:
            return None

        odds_dir = Direction.DOWN if market.down_odds >= market.up_odds else Direction.UP
        odds_px  = market.down_odds if odds_dir == Direction.DOWN else market.up_odds
        token_id = market.down_token if odds_dir == Direction.DOWN else market.up_token

        timing_ok     = (state.odds_jumped and minute >= 1) or minute >= min_min
        gap_conflicts = (
            abs(gap) >= 0.05 and
            ((odds_dir == Direction.DOWN and gap > 0) or
             (odds_dir == Direction.UP   and gap < 0))
        )
        if not timing_ok or gap_conflicts:
            return None

        if secs_remaining < PATH2_MIN_SECS_REMAINING:
            logger.debug(
                "路径2 剩余时间过短: %ds < %ds",
                secs_remaining, PATH2_MIN_SECS_REMAINING,
            )
            return None

        # ── P0优化1：低波动环境提高赔率门槛 ──────────────────────────
        # 数据依据：实盘亏损单中80%发生在"波动↓"环境
        # 低波动时，路径2最低赔率从0.78提高到0.85
        if vol_level == "low" and odds_px < 0.85:
            logger.debug(
                "低波动环境拦截: vol=%s odds=%.2f < 0.85",
                vol_level, odds_px,
            )
            return None

        # ── P1：跳变冷却期 ──────────────────────────────────────────
        # 基础冷却 15s：防止假跳变（实盘04:38案例：+0.25后10s内连续反向跳变）
        # 连续跳变延长：若同向跳变次数≥2，冷却延长至20s（实盘05:47案例：
        #   -0.16 → -0.17 两次同向跳变后26s下单，下单后8s出现+0.26反向跳变）
        # 注意：30s太长会错过整个分3信号窗口（5分钟市场时间宝贵），折衷为20s
        BASE_COOLDOWN  = 15
        MULTI_COOLDOWN = 20
        if state.odds_jumped and state.jump_ts > 0:
            secs_since_jump = time.time() - state.jump_ts
            jump_count = getattr(state, 'jump_count', 1)
            cooldown = MULTI_COOLDOWN if jump_count >= 2 else BASE_COOLDOWN
            if secs_since_jump < cooldown:
                logger.debug(
                    "跳变冷却拦截: 跳变%d次 距上次%.0fs < %ds",
                    jump_count, secs_since_jump, cooldown,
                )
                return None

        gap_same_dir = (
            (odds_dir == Direction.DOWN and gap < 0) or
            (odds_dir == Direction.UP   and gap > 0)
        )

        # ── P0优化2：震荡市双确认加强 ──────────────────────────────
        # 数据依据：实盘#326/#327在震荡市（conf=0.48）下单后反向
        # 震荡市（conf<0.55）要求：赔率≥0.85 或 gap同向≥0.10%
        if minute <= 3 and signal_confidence < 0.55:
            if odds_px < 0.85 and not (gap_same_dir and abs(gap) >= 0.10):
                logger.debug(
                    "震荡市拦截: conf=%.2f odds=%.2f gap=%.3f%% (需赔率≥0.85或gap同向≥0.10%%)",
                    signal_confidence, odds_px, gap,
                )
                return None

        # ── 早期分钟分层拦截 ────────────────────────────────────────
        if state.odds_jumped and minute <= 2:
            if minute == 1:
                # 分1：禁止 TWR=0.860（赔率 < 0.85）信号
                # 数据：分1 整体胜率 73.1%，TWR=0.860 贡献大部分亏损
                if odds_px < 0.85:
                    logger.debug(
                        "分1 赔率强信号拦截: odds=%.2f < 0.85 (TWR=0.860 胜率不足，禁止分1入场)",
                        odds_px,
                    )
                    return None
                # 分1 高赔率（≥0.85，TWR=0.968）：仍需跳变 + 高可信度
                min_conf = 0.55
                if signal_confidence < min_conf:
                    logger.debug(
                        "分1 高赔率信号拦截: conf=%.2f < %.2f",
                        signal_confidence, min_conf,
                    )
                    return None

            else:  # minute == 2
                # 分2 UP：胜率 65%，要求 price<0.80 且 gap 同向 >= 0.15%
                if odds_dir == Direction.UP:
                    if odds_px >= 0.80 or not (gap_same_dir and abs(gap) >= 0.15):
                        logger.debug(
                            "分2 UP 信号拦截: odds=%.2f (需<0.80), gap=%.3f%% same=%s (需同向≥0.15%%)",
                            odds_px, gap, gap_same_dir,
                        )
                        return None
                # 分2 DOWN：维持原始门槛（胜率 90.5%）
                else:
                    min_gap_dn, min_odds_dn = 0.01, 0.75
                    # 默认 conf≥0.35；若赔率≥0.85 且 gap 与 Down 同向，可放宽至 0.28
                    # （保留 02:32 高赔率 Down 成功案例，同时拦截 0.79/#326 类弱势逆势单）
                    min_conf_dn = (
                        0.28 if (odds_px >= 0.85 and gap_same_dir) else 0.35
                    )
                    gap_ok   = gap_same_dir and abs(gap) >= min_gap_dn
                    odds_ok  = odds_px >= min_odds_dn
                    conf_ok  = signal_confidence >= min_conf_dn
                    if not ((gap_ok or odds_ok) and conf_ok):
                        logger.debug(
                            "分2 DOWN 信号拦截: gap=%.3f%% odds=%.2f conf=%.2f",
                            gap, odds_px, signal_confidence,
                        )
                        return None

                # 通用：低可信度时收紧
                if signal_confidence < 0.35:
                    logger.debug("分2 低可信度拦截: conf=%.2f", signal_confidence)
                    return None

        # ── 路径2仅允许高赔率（≥0.85，TWR=0.968）────────────────────
        # ── price_max 上限（参考4coinsbot：赔率过高利润太薄）──
        if odds_px > price_max:
            logger.debug("路径2 price_max 拦截: odds=%.3f > %.2f", odds_px, price_max)
            return None

        # ── 理论胜率（由852窗口回测数据校准，比之前更精确）──
        if odds_px >= 0.85:
            theo_wr = 0.960
        elif odds_px >= 0.78:
            theo_wr = 0.920
        else:
            theo_wr = 0.850

        ev       = theo_wr * (1 - odds_px) - (1 - theo_wr) * odds_px
        fee_frac = 0.25 * (odds_px * (1 - odds_px)) ** 2
        ev_fee   = ev - theo_wr * fee_frac

        # ── P1优化：EV 门槛动态调整 ──────────────────────────────
        # 根据市场环境动态调整EV门槛：
        #   - 低波动环境：EV门槛×2（数据：80%亏损单发生在低波动）
        #   - 低可信度（<0.50）：EV门槛×1.5
        #   - 23点高波动时段：EV门槛×1.75
        _local_hour = time.localtime().tm_hour
        base_ev      = gp["min_ev"]

        if vol_level == "low":
            ev_threshold = base_ev * 2.0  # 低波动：最严格
        elif _local_hour == 23:
            ev_threshold = base_ev * 1.75  # 23点高波动
        elif signal_confidence < 0.50:
            ev_threshold = base_ev * 1.5  # 低可信度
        else:
            ev_threshold = base_ev

        if ev < ev_threshold:
            logger.debug(
                "EV拦截: ev=%.4f < %.4f (vol=%s conf=%.2f greed=%d hour=%dh)",
                ev, ev_threshold, vol_level, signal_confidence, cfg.greed_index, _local_hour
            )
            return None

        # ── P0优化3：分3高赔率双确认加强 ────────────────────────────
        # 实盘案例（#319 Up@0.78 gap+0.02% -$4.95；#325 Down@0.785 gap-0.01% -$3.92）：
        #   赔率0.78~0.88，分3，gap≈0，最终反向结算
        # 优化：gap门槛从0.05提高到0.10，conf门槛从0.55提高到0.60
        if minute >= 3 and odds_px > 0.85:
            gap_ok  = gap_same_dir and abs(gap) >= 0.10  # 从0.05提高到0.10
            conf_ok = signal_confidence >= 0.60
            if not (gap_ok and conf_ok):
                logger.debug(
                    "分3高赔率双确认拦截: odds=%.2f gap=%.3f%%(same=%s) conf=%.2f (需gap同向≥0.10%%且conf≥0.60)",
                    odds_px, gap, gap_same_dir, signal_confidence,
                )
                return None

        # ── Kelly 注额：低赔率机会允许更高上限 ──────────────────────
        kelly     = self._kelly_with_low_odds(theo_wr, odds_px)
        jump_note = "跳变+" if state.odds_jumped else ""
        if abs(gap) < 0.01:
            gap_note = "gap≈0"
        elif gap_same_dir:
            gap_note = f"gap同向{gap:+.3f}%"
        else:
            gap_note = f"gap反向{gap:+.3f}%⚠"
        sig = Signal(
            direction=odds_dir, token_id=token_id,
            condition_id=market.condition_id,
            theoretical_win_rate=theo_wr, market_price=odds_px,
            ev_per_unit=ev, ev_after_fee=ev_fee, gap_pct=gap, gap_src=gap_src,
            seconds_remaining=secs_remaining, kelly_fraction=kelly,
            bet_amount=self.total_capital * kelly, signal_type="跟赔率",
            note=f"{jump_note}赔率={odds_px:.2f} {gap_note}",
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
    def _kelly_with_low_odds(win_rate: float, entry_price: float) -> float:
        """
        低赔率机会（market_price < low_odds_thresh）允许更高的 Kelly 上限。
        数据依据：entry_price < 0.55 时实际胜率 96.4%（55/57 笔），可适度加仓。
        """
        b = (1 - entry_price) / entry_price
        if b <= 0:
            return 0.0
        kelly = (b * win_rate - (1 - win_rate)) / b
        # 低赔率机会用 high_conf_bet_fraction，否则用 max_bet_fraction
        cap = cfg.high_conf_bet_fraction if entry_price < cfg.low_odds_thresh else cfg.max_bet_fraction
        return max(0.0, min(kelly * 0.5, cap))

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

    def sweep_capital(self, amount_swept: float):
        """
        资金归集后同步资金基准。
        同时更新 current_capital 和 _day_start_capital，
        避免归集导致 day_loss 虚高触发熔断。
        """
        self.current_capital    -= amount_swept
        self._day_start_capital -= amount_swept

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

    def daily_reset(self, new_capital: float):
        """
        每日重置：用链上最新余额刷新资金基准，清空胜率统计和熔断状态。
        保留 current_capital 的历史记录（通过 new_capital 传入）。
        """
        prev_stats = self.stats
        self.initial_capital     = new_capital
        self.current_capital     = new_capital
        self._day_start_capital  = new_capital
        self._day_start_ts       = self._today_ts()
        self._consecutive_losses = 0
        self._paused_until       = 0.0
        self._total_trades       = 0
        self._total_wins         = 0
        return prev_stats   # 返回重置前的统计，供日报使用

    def _refresh_day(self):
        today = self._today_ts()
        if today > self._day_start_ts:
            self._day_start_capital = self.current_capital
            self._day_start_ts      = today

    @staticmethod
    def _today_ts() -> int:
        now = int(time.time())
        return now - (now % 86400)
