"""
Polymarket BTC 5分钟涨跌预测 - 策略回测框架
基于 Binance BTC/USDT 1分钟 K 线模拟 Polymarket 5分钟窗口
"""

import json
import math
from dataclasses import dataclass, field
from typing import Optional


# ─────────────────────────────────────────────
# 数据加载
# ─────────────────────────────────────────────

def load_candles(path: str) -> list[dict]:
    with open(path) as f:
        raw = json.load(f)
    candles = []
    for r in raw:
        candles.append({
            "ts":    int(r[0]) // 1000,   # 秒
            "open":  float(r[1]),
            "high":  float(r[2]),
            "low":   float(r[3]),
            "close": float(r[4]),
            "vol":   float(r[5]),
        })
    return candles


def build_5m_windows(candles: list[dict]) -> list[dict]:
    """
    将1分钟K线聚合为5分钟窗口列表。
    窗口时间戳对齐到 ts % 300 == 0。
    返回字段：
      window_open_ts, open_price, close_price,
      result ("Up"/"Down"), candles_in_window (5条1m K线列表)
    """
    by_ts = {c["ts"]: c for c in candles}
    min_ts = candles[0]["ts"]
    max_ts = candles[-1]["ts"]

    # 找第一个对齐的窗口
    first_window_ts = min_ts - (min_ts % 300) + 300
    windows = []
    ts = first_window_ts
    while ts + 300 <= max_ts:
        # 窗口内5根1m K线：ts, ts+60, ts+120, ts+180, ts+240
        slot_candles = []
        for offset in range(0, 300, 60):
            c = by_ts.get(ts + offset)
            if c:
                slot_candles.append(c)

        # 要求完整5根
        if len(slot_candles) == 5:
            open_price  = slot_candles[0]["open"]
            close_price = slot_candles[-1]["close"]
            result = "Up" if close_price >= open_price else "Down"
            windows.append({
                "ts":        ts,
                "open":      open_price,
                "close":     close_price,
                "high":      max(c["high"] for c in slot_candles),
                "low":       min(c["low"]  for c in slot_candles),
                "result":    result,
                "sub":       slot_candles,
            })
        ts += 300
    return windows


# ─────────────────────────────────────────────
# 策略基类
# ─────────────────────────────────────────────

@dataclass
class Signal:
    direction: str          # "Up" | "Down" | "PASS"
    confidence: float       # 0~1，用于 Kelly 仓位计算
    entry_price: float      # 预估入场价格（模拟赔率）
    strategy: str = ""

    @property
    def should_trade(self) -> bool:
        return self.direction != "PASS"


@dataclass
class TradeResult:
    strategy: str
    window_ts: int
    direction: str
    entry_price: float
    result: str
    win: bool
    pnl: float              # 以单位仓位计算，entry_price=1


# ─────────────────────────────────────────────
# 策略实现
# ─────────────────────────────────────────────

def strategy_momentum(window: dict, all_candles: list[dict], idx: int,
                      lookback_secs: int = 60, threshold_pct: float = 0.05) -> Signal:
    """
    动量策略：窗口开始前 lookback_secs 秒的价格变化方向预测下一窗口。
    threshold_pct：触发信号所需的最小变化幅度（百分比）
    入场时机：窗口前30秒（早入场，赔率约 50/50）
    """
    win_ts = window["ts"]
    # 找窗口开始前的K线
    target_ts = win_ts - lookback_secs
    prev_close = None
    for c in reversed(all_candles[:idx * 5]):  # 粗略索引
        if c["ts"] <= target_ts:
            prev_close = c["close"]
            break
    if prev_close is None:
        return Signal("PASS", 0, 0.5, "momentum")

    current_price = window["open"]  # 窗口开始时价格
    change_pct = (current_price - prev_close) / prev_close * 100

    if change_pct > threshold_pct:
        confidence = min(0.6 + abs(change_pct) * 2, 0.75)
        return Signal("Up", confidence, 0.52, "momentum")
    elif change_pct < -threshold_pct:
        confidence = min(0.6 + abs(change_pct) * 2, 0.75)
        return Signal("Down", confidence, 0.52, "momentum")
    else:
        return Signal("PASS", 0, 0.5, "momentum")


def strategy_reversal(window: dict, windows: list[dict], win_idx: int,
                      streak_threshold: int = 2) -> Signal:
    """
    均值回归策略：连续 N 次同向后押相反方向。
    思路：5分钟级别价格趋势难以长期维持一个方向，
    连涨/连跌后反转概率稍高于 50%。
    """
    if win_idx < streak_threshold:
        return Signal("PASS", 0, 0.5, "reversal")

    recent = [windows[win_idx - 1 - i]["result"] for i in range(streak_threshold)]
    if all(r == "Up" for r in recent):
        return Signal("Down", 0.55, 0.52, "reversal")
    elif all(r == "Down" for r in recent):
        return Signal("Up", 0.55, 0.52, "reversal")
    return Signal("PASS", 0, 0.5, "reversal")


def strategy_late_stage(window: dict, enter_at_minute: int = 4,
                        min_gap_pct: float = 0.03) -> Signal:
    """
    末期套利策略：在窗口第 N 分钟，根据实时价格与开盘价的差距下注。
    只有当价差足够大（>min_gap_pct%）时才入场，胜率高但赔率差。

    注意：现实中入场在第4分钟，Polymarket 赔率已高度倾斜。
    这里模拟入场价格：差距越大，赔率越接近 1，意味着利润越薄。
    """
    sub = window["sub"]
    if enter_at_minute > len(sub):
        return Signal("PASS", 0, 0.5, "late_stage")

    price_at_entry = sub[enter_at_minute - 1]["close"]
    open_price = window["open"]
    gap_pct = (price_at_entry - open_price) / open_price * 100

    if gap_pct > min_gap_pct:
        # 价格领先，赔率已高，利润薄
        # 模拟入场价格：gap越大，市场赔率越高（越贵）
        entry = min(0.70 + abs(gap_pct) * 5, 0.93)
        confidence = min(0.72 + abs(gap_pct) * 3, 0.92)
        return Signal("Up", confidence, entry, "late_stage")
    elif gap_pct < -min_gap_pct:
        entry = min(0.70 + abs(gap_pct) * 5, 0.93)
        confidence = min(0.72 + abs(gap_pct) * 3, 0.92)
        return Signal("Down", confidence, entry, "late_stage")
    return Signal("PASS", 0, 0.5, "late_stage")


def strategy_momentum_with_vol(window: dict, all_candles: list[dict], idx: int,
                                lookback_mins: int = 5,
                                vol_multiplier: float = 0.8) -> Signal:
    """
    波动率归一化动量：
    动量信号 / ATR(5m) > vol_multiplier 时才触发。
    避免在低波动时产生假信号。
    """
    offset = idx * 5
    if offset < lookback_mins + 5:
        return Signal("PASS", 0, 0.5, "mom_vol")

    recent_closes = [all_candles[offset - i - 1]["close"] for i in range(lookback_mins)]
    recent_closes.reverse()

    momentum = recent_closes[-1] - recent_closes[0]
    # ATR 估算：取最近 N 根 K 线的 high-low 均值
    atr_candles = all_candles[max(0, offset - 10): offset]
    atr = sum(c["high"] - c["low"] for c in atr_candles) / max(len(atr_candles), 1)

    if atr == 0:
        return Signal("PASS", 0, 0.5, "mom_vol")

    norm_mom = momentum / atr
    if norm_mom > vol_multiplier:
        confidence = min(0.58 + norm_mom * 0.05, 0.72)
        return Signal("Up", confidence, 0.52, "mom_vol")
    elif norm_mom < -vol_multiplier:
        confidence = min(0.58 + abs(norm_mom) * 0.05, 0.72)
        return Signal("Down", confidence, 0.52, "mom_vol")
    return Signal("PASS", 0, 0.5, "mom_vol")


# ─────────────────────────────────────────────
# 仓位管理：Kelly 公式
# ─────────────────────────────────────────────

def kelly_fraction(win_rate: float, odds_payout: float,
                   max_fraction: float = 0.05) -> float:
    """
    Kelly = (b*p - q) / b
    win_rate: 预期胜率
    odds_payout: 赔率（以1为本金的盈利倍数）= (1 - entry_price) / entry_price
    """
    b = odds_payout
    p = win_rate
    q = 1 - p
    kelly = (b * p - q) / b if b > 0 else 0
    return max(0.0, min(kelly * 0.5, max_fraction))  # 半 Kelly，上限 5%


# ─────────────────────────────────────────────
# 回测引擎
# ─────────────────────────────────────────────

def run_backtest(windows: list[dict], all_candles: list[dict],
                 strategies: list[str],
                 initial_capital: float = 1000.0) -> dict:
    """
    对每个5分钟窗口运行所有策略，统计盈亏。
    """
    results = {s: [] for s in strategies}
    equity  = {s: initial_capital for s in strategies}
    equity_curve = {s: [initial_capital] for s in strategies}

    # 按时间排序
    for win_idx, window in enumerate(windows):
        if win_idx < 10:
            continue  # 预热期

        for strat in strategies:
            if strat == "momentum":
                signal = strategy_momentum(window, all_candles, win_idx)
            elif strat == "reversal":
                signal = strategy_reversal(window, windows, win_idx)
            elif strat == "late_stage":
                signal = strategy_late_stage(window)
            elif strat == "mom_vol":
                signal = strategy_momentum_with_vol(window, all_candles, win_idx)
            else:
                continue

            if not signal.should_trade:
                equity_curve[strat].append(equity[strat])
                continue

            # Kelly 仓位
            odds = (1 - signal.entry_price) / signal.entry_price
            bet_fraction = kelly_fraction(signal.confidence, odds)
            bet_amount = equity[strat] * bet_fraction

            win = signal.direction == window["result"]
            if win:
                pnl = bet_amount * (1 - signal.entry_price) / signal.entry_price
            else:
                pnl = -bet_amount

            equity[strat] += pnl
            equity_curve[strat].append(equity[strat])

            results[strat].append(TradeResult(
                strategy=strat,
                window_ts=window["ts"],
                direction=signal.direction,
                entry_price=signal.entry_price,
                result=window["result"],
                win=win,
                pnl=pnl,
            ))

    return {
        "trades": results,
        "equity": equity,
        "equity_curve": equity_curve,
        "initial": initial_capital,
    }


# ─────────────────────────────────────────────
# 统计分析
# ─────────────────────────────────────────────

def analyze(backtest_result: dict) -> None:
    initial = backtest_result["initial"]
    print("=" * 65)
    print(f"{'策略':<12} {'交易次数':>6} {'胜率':>7} {'总收益率':>9} "
          f"{'均盈/亏':>9} {'最大回撤':>9} {'Sharpe':>7}")
    print("-" * 65)

    for strat, trades in backtest_result["trades"].items():
        if not trades:
            print(f"{strat:<12} {'0':>6} {'N/A':>7} {'N/A':>9}")
            continue

        total = len(trades)
        wins = sum(1 for t in trades if t.win)
        win_rate = wins / total
        total_pnl = sum(t.pnl for t in trades)
        total_return = total_pnl / initial * 100
        avg_win  = sum(t.pnl for t in trades if t.win) / max(wins, 1)
        avg_loss = sum(t.pnl for t in trades if not t.win) / max(total - wins, 1)

        # 最大回撤
        curve = backtest_result["equity_curve"][strat]
        peak = curve[0]
        max_dd = 0.0
        for v in curve:
            if v > peak:
                peak = v
            dd = (peak - v) / peak
            if dd > max_dd:
                max_dd = dd

        # Sharpe（简化：均收益/收益标准差）
        pnls = [t.pnl for t in trades]
        avg_pnl = sum(pnls) / len(pnls)
        std_pnl = math.sqrt(sum((p - avg_pnl) ** 2 for p in pnls) / len(pnls))
        sharpe = (avg_pnl / std_pnl) * math.sqrt(total) if std_pnl > 0 else 0

        print(f"{strat:<12} {total:>6} {win_rate:>7.1%} {total_return:>8.1f}% "
              f"{avg_win:>7.2f} / {avg_loss:>6.2f}  {max_dd:>7.1%}  {sharpe:>7.2f}")

    print("=" * 65)

    # 末期套利详细分析
    late = backtest_result["trades"].get("late_stage", [])
    if late:
        print("\n── 末期套利按入场价区间细分 ──")
        buckets = {}
        for t in late:
            bucket = round(t.entry_price * 10) / 10
            if bucket not in buckets:
                buckets[bucket] = []
            buckets[bucket].append(t)
        for k in sorted(buckets):
            ts = buckets[k]
            wr = sum(1 for t in ts if t.win) / len(ts)
            expected = wr * (1 - k) - (1 - wr) * k
            print(f"  入场价 {k:.1f}: 次数={len(ts):3d}  胜率={wr:.1%}  期望值/单位={expected:+.4f}")

    # 动量策略按信号强度分析
    print("\n── 动量策略按窗口时段分析（0-24小时） ──")
    mom = backtest_result["trades"].get("momentum", [])
    if mom:
        by_hour = {}
        for t in mom:
            hour = (t.window_ts % 86400) // 3600
            by_hour.setdefault(hour, []).append(t)
        for h in sorted(by_hour):
            ts = by_hour[h]
            wr = sum(1 for t in ts if t.win) / len(ts)
            print(f"  UTC {h:02d}h: 次数={len(ts):3d}  胜率={wr:.1%}", end="")
            if wr > 0.55:
                print(" ← 优势时段")
            elif wr < 0.45:
                print(" ← 弱势时段")
            else:
                print()


# ─────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────

def main():
    print("Loading candles...")
    candles = load_candles("btc_1m_7d.json")
    print(f"  {len(candles)} 条1分钟K线，时间跨度: {(candles[-1]['ts'] - candles[0]['ts']) / 86400:.1f} 天")

    print("Building 5-minute windows...")
    windows = build_5m_windows(candles)
    print(f"  {len(windows)} 个5分钟窗口")

    up_count = sum(1 for w in windows if w["result"] == "Up")
    print(f"  Up={up_count} ({up_count/len(windows):.1%})  Down={len(windows)-up_count} ({(len(windows)-up_count)/len(windows):.1%})")
    print()

    strategies = ["momentum", "mom_vol", "reversal", "late_stage"]
    print(f"Running backtest on {len(strategies)} strategies...")
    result = run_backtest(windows, candles, strategies, initial_capital=1000.0)

    print()
    analyze(result)

    # 打印最终资金
    print("\n── 最终资金（初始 $1000） ──")
    for strat, eq in result["equity"].items():
        change = eq - 1000
        print(f"  {strat:<12}: ${eq:,.2f}  ({change:+.2f})")


if __name__ == "__main__":
    main()
