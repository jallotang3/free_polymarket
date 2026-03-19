"""
历史结算结果补填脚本

将 observations.db 中所有已结束窗口的 UP/DOWN 结算结果写入 window_results 表。
运行一次即可，之后由 collect_data.py 自动维护。

用法：
    python scripts/backfill_results.py
    python scripts/backfill_results.py --limit 50    # 只补最近50个窗口
    python scripts/backfill_results.py --dry-run      # 只查询不写入
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root))

try:
    from dotenv import load_dotenv
    load_dotenv(_root / ".env")
except ImportError:
    pass

DB_PATH = str(_root / "observations.db")


def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    px = os.environ.get("PROXY_URL", "").strip()
    if px:
        s.proxies = {"http": px, "https": px}
    return s


def fetch_result(session: requests.Session, window_ts: int) -> str | None:
    """查询单个窗口的结算结果，返回 'Up' / 'Down' / None。"""
    slug = f"btc-updown-5m-{window_ts}"
    try:
        resp = session.get(
            f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=8
        )
        data = resp.json()
        if not data:
            return None
        m      = data[0]["markets"][0]
        closed = m.get("closed", False)
        if not closed:
            return None
        prices = json.loads(m["outcomePrices"])
        up_p   = float(prices[0])
        dn_p   = float(prices[1])
        if up_p >= 0.99:
            return "Up"
        elif dn_p >= 0.99:
            return "Down"
        return None
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description="补填历史结算结果")
    parser.add_argument("--limit",   type=int, default=0,     help="最多处理N个窗口（0=全部）")
    parser.add_argument("--dry-run", action="store_true",     help="只查询不写入数据库")
    args = parser.parse_args()

    conn    = sqlite3.connect(DB_PATH)
    session = _build_session()

    # 已记录的窗口
    recorded = {r[0] for r in conn.execute("SELECT window_ts FROM window_results").fetchall()}
    print(f"已记录结算结果：{len(recorded)} 个窗口")

    # 需要补填的窗口（结束超过10s的）
    cutoff = int(time.time()) - 310
    cur = conn.execute("""
        SELECT DISTINCT window_ts FROM observations
        WHERE window_ts < ?
        ORDER BY window_ts DESC
    """, (cutoff,))
    all_windows = [r[0] for r in cur.fetchall()]
    pending     = [w for w in all_windows if w not in recorded]

    if args.limit > 0:
        pending = pending[:args.limit]

    print(f"待补填窗口：{len(pending)} 个（共 {len(all_windows)} 个历史窗口）")
    if not pending:
        print("没有需要补填的窗口。")
        conn.close()
        return

    import datetime
    success = fail = skip = 0
    for i, wts in enumerate(pending, 1):
        wt_str = datetime.datetime.fromtimestamp(wts).strftime("%m-%d %H:%M")
        result = fetch_result(session, wts)

        if result:
            if not args.dry_run:
                conn.execute("""
                    INSERT OR REPLACE INTO window_results (window_ts, result, recorded_at)
                    VALUES (?, ?, ?)
                """, (wts, result, int(time.time())))
                conn.commit()
            status = f"✅ {result}"
            success += 1
        else:
            status = "⏳ 未结算/查询失败"
            fail += 1

        print(f"  [{i:3d}/{len(pending)}] {wt_str} → {status}")

        # 限速：每次查询后等 0.3s，避免触发 Gamma API 频率限制
        time.sleep(0.3)

    print(f"\n补填完成：成功={success}  失败/未结算={fail}")
    if args.dry_run:
        print("（dry-run 模式，未写入数据库）")

    # 统计最终结果分布
    total_recorded = conn.execute("SELECT COUNT(*) FROM window_results").fetchone()[0]
    up_cnt   = conn.execute("SELECT COUNT(*) FROM window_results WHERE result='Up'").fetchone()[0]
    down_cnt = conn.execute("SELECT COUNT(*) FROM window_results WHERE result='Down'").fetchone()[0]
    print(f"\n当前 window_results：{total_recorded} 条  Up={up_cnt}  Down={down_cnt}")
    conn.close()


if __name__ == "__main__":
    main()
