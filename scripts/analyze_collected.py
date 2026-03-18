"""
Phase 1 数据分析脚本

从 collect_data.py 采集的 observations.db 中提取关键指标，
判断末期套利策略在当前市场条件下是否可行。

用法：
  python scripts/analyze_collected.py
  python scripts/analyze_collected.py --db data/observations.db
"""

import argparse
import sqlite3
import sys
from pathlib import Path

# ────────────────────────────────────────────
_root = Path(__file__).parent.parent
# collect_data.py 默认在项目根目录创建 observations.db
_DEFAULT_DB = _root / "observations.db"


def load_obs(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM observations ORDER BY ts").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def load_trades(db_path: str) -> list[dict]:
    """加载 bot.py 产生的交易记录（如果存在）"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM trades ORDER BY opened_at").fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def _best_gap(r: dict) -> float | None:
    """
    选取最可靠的 gap 值（与 collect_data.py 优先级一致）：
    Chainlink 链上 > CryptoCompare(gap_pct) > Binance(bn_gap_pct)
    """
    cl = r.get("cl_onchain_gap_pct")
    if cl is not None:
        return cl
    return r.get("gap_pct")


def analyze(obs: list[dict]) -> None:
    if not obs:
        print("没有数据，请先运行 scripts/collect_data.py 采集数据。")
        return

    total = len(obs)
    print(f"\n{'='*60}")
    print(f"  Phase 1 数据分析报告")
    print(f"{'='*60}")
    print(f"总观察条数: {total}")
    first_ts = obs[0]["ts"]
    last_ts  = obs[-1]["ts"]
    hours = (last_ts - first_ts) / 3600
    print(f"覆盖时间: {hours:.1f} 小时 / 约 {hours/24:.1f} 天")

    # Chainlink 链上数据覆盖率统计
    cl_count = sum(1 for r in obs if r.get("cl_onchain_gap_pct") is not None)
    print(f"Chainlink链上gap 覆盖率: {cl_count}/{total} ({cl_count/total:.0%})")

    # CL vs CC 偏差统计（有助于评估 gap 精度）
    both = [
        r for r in obs
        if r.get("cl_onchain_gap_pct") is not None and r.get("gap_pct") is not None
    ]
    if both:
        diffs = [abs(r["cl_onchain_gap_pct"] - r["gap_pct"]) for r in both]
        avg_diff = sum(diffs) / len(diffs)
        print(f"CL链上 vs CC 平均gap偏差: {avg_diff:.4f}%  "
              f"(偏差>0.05%的比例: {sum(1 for d in diffs if d > 0.05)/len(diffs):.0%})")
    print()

    # ── 核心分析：入场窗口内（第3~4分钟）的市场定价 ──
    print("── 末期套利机会分析（入场窗口：3:30~4:30） ──\n")

    for gap_threshold in [0.05, 0.10, 0.15, 0.20]:
        # 理论胜率（基于7天回测）
        theo_wr_map = {0.05: 0.897, 0.10: 0.968, 0.15: 0.979, 0.20: 0.982}
        theo_wr = theo_wr_map[gap_threshold]

        # 筛选：入场窗口内 + 最佳gap满足阈值
        matching = [
            r for r in obs
            if r["minute_in_window"] in (3, 4)
            and _best_gap(r) is not None
            and abs(_best_gap(r)) >= gap_threshold
        ]
        if len(matching) < 5:
            continue

        # 标注 CL 数据占比
        cl_in_match = sum(1 for r in matching if r.get("cl_onchain_gap_pct") is not None)

        # 获取对应方向的市场赔率
        market_prices = []
        for r in matching:
            g = _best_gap(r)
            mp = r["up_odds"] if g > 0 else r["down_odds"]
            if mp and 0 < mp < 1:
                market_prices.append(mp)

        if not market_prices:
            continue

        market_prices.sort()
        n = len(market_prices)
        median = market_prices[n // 2]
        avg    = sum(market_prices) / n
        p25    = market_prices[n // 4]
        p75    = market_prices[3 * n // 4]

        ev_median = theo_wr * (1 - median) - (1 - theo_wr) * median
        ev_avg    = theo_wr * (1 - avg)    - (1 - theo_wr) * avg

        profitable = sum(1 for mp in market_prices if mp < theo_wr)
        profitable_pct = profitable / n

        print(f"  gap ≥ {gap_threshold:.2f}% | 理论胜率={theo_wr:.1%}  "
              f"[CL链上占比: {cl_in_match}/{n}]")
        print(f"  样本={n}  市场价格: 中位={median:.3f}  均值={avg:.3f}  "
              f"Q25={p25:.3f}  Q75={p75:.3f}")
        print(f"  EV(中位)={ev_median:+.4f}  EV(均值)={ev_avg:+.4f}  "
              f"可盈利比例={profitable_pct:.1%}")

        if ev_median > 0.03:
            verdict = "🟢 强套利机会"
        elif ev_median > 0.01:
            verdict = "🟡 弱套利机会"
        elif ev_median > 0:
            verdict = "⚪ 微弱优势"
        else:
            verdict = "🔴 市场已充分定价，无套利空间"
        print(f"  结论: {verdict}\n")

    # ── 时段分析 ──
    print("── 各时段（UTC）套利机会分布 ──\n")
    hour_data: dict[int, list[float]] = {}
    for r in obs:
        g = _best_gap(r)
        if r["minute_in_window"] in (3, 4) and g and abs(g) >= 0.10:
            hour = (r["ts"] % 86400) // 3600
            mp = r["up_odds"] if g > 0 else r["down_odds"]
            if mp and 0 < mp < 1:
                hour_data.setdefault(hour, []).append(mp)

    best_hours = []
    for h in sorted(hour_data):
        prices = hour_data[h]
        n = len(prices)
        avg = sum(prices) / n
        ev = 0.968 * (1 - avg) - 0.032 * avg
        if n >= 3:
            marker = " ← 优势时段" if ev > 0.03 else ""
            print(f"  UTC {h:02d}h: n={n:3d}  avg_mkt={avg:.3f}  EV={ev:+.4f}{marker}")
            if ev > 0.03:
                best_hours.append(h)

    if best_hours:
        print(f"\n  最优入场时段（UTC）: {best_hours}")

    # ── 最终建议 ──
    print(f"\n{'='*60}")
    print("  最终建议")
    print(f"{'='*60}")

    all_gap10 = [
        r["up_odds"] if _best_gap(r) > 0 else r["down_odds"]
        for r in obs
        if r["minute_in_window"] in (3, 4)
        and _best_gap(r) is not None and abs(_best_gap(r)) >= 0.10
        and 0 < (r["up_odds"] if _best_gap(r) > 0 else r["down_odds"]) < 1
    ]

    if not all_gap10:
        print("  数据不足，请继续采集。")
        return

    all_gap10.sort()
    n = len(all_gap10)
    overall_median = all_gap10[n // 2]
    ev = 0.968 * (1 - overall_median) - 0.032 * overall_median

    print(f"  基于 {n} 条末期套利机会观察:")
    print(f"  市场定价中位数 = {overall_median:.3f}")
    print(f"  期望收益 = {ev:+.4f} / 单位")
    print()

    if ev > 0.03:
        print("  ✅ 策略可行！建议进入 Phase 2（纸面交易测试）")
        print("     运行命令: python -m src.bot --mode paper --capital 1000")
    elif ev > 0.01:
        print("  🟡 策略勉强可行，利润空间较窄，建议继续采集更多数据验证")
    elif ev > 0:
        print("  ⚠️  期望收益过低（< 1¢/单位），扣除 gas 费后可能亏损")
        print("     建议等待市场流动性较低时段，或提高 gap 阈值")
    else:
        print("  ❌ 当前市场定价已充分反映胜率，末期套利无利可图")
        print("     建议重新审视策略，或等待市场条件改变")


def analyze_trades(trades: list[dict]) -> None:
    """分析机器人的交易记录"""
    closed = [t for t in trades if t["status"] in ("win", "loss")]
    if not closed:
        print("\n暂无已结算的交易记录。")
        return

    total = len(closed)
    wins = sum(1 for t in closed if t["status"] == "win")
    total_pnl = sum(t["pnl"] or 0 for t in closed)
    avg_entry = sum(t["entry_price"] for t in closed) / total

    print(f"\n{'='*60}")
    print("  交易记录统计")
    print(f"{'='*60}")
    print(f"  已结算交易: {total}")
    print(f"  胜率: {wins/total:.1%}  ({wins}W/{total-wins}L)")
    print(f"  总 PnL: {total_pnl:+.4f} USDC")
    print(f"  平均入场价: {avg_entry:.3f}")

    # 对比理论期望
    avg_ev = sum(t.get("ev_per_unit") or 0 for t in closed) / total
    print(f"  平均理论 EV: {avg_ev:+.4f}")
    print(f"  实际 EV: {total_pnl / sum(t['amount_usdc'] for t in closed):+.4f}")

    # 模式分布
    modes = {}
    for t in closed:
        modes[t["mode"]] = modes.get(t["mode"], 0) + 1
    mode_str = "  ".join(f"{k}={v}" for k, v in modes.items())
    print(f"  模式: {mode_str}")


def main():
    parser = argparse.ArgumentParser(description="Phase 1 数据分析")
    parser.add_argument("--db", default=str(_DEFAULT_DB), help="数据库路径")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"数据库不存在: {args.db}")
        print("请先运行: python scripts/collect_data.py")
        sys.exit(1)

    obs = load_obs(args.db)
    analyze(obs)

    trades = load_trades(args.db)
    if trades:
        analyze_trades(trades)


if __name__ == "__main__":
    main()
