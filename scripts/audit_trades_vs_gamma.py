#!/usr/bin/env python3
"""
事后审计：将 SQLite「trades」表中的 window_ts 与 Gamma API 的 eventMetadata 对照。

说明：
  - priceToBeat / finalPrice 多在结算后才有（与 bot 盘中锚价不是同一时刻的数据源）。
  - 本脚本用于量化「官方 Up/Down」与「你当时用的 gap 方向」是否一致，需结合日志里的 gap 理解。

用法：
  python scripts/audit_trades_vs_gamma.py
  python scripts/audit_trades_vs_gamma.py --db /path/to/data/observations.db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
GAMMA = "https://gamma-api.polymarket.com"


def fetch_gamma(window_ts: int) -> dict | None:
    slug = f"btc-updown-5m-{window_ts}"
    try:
        r = requests.get(f"{GAMMA}/events?slug={slug}", timeout=12)
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        ev = data[0]
        meta = ev.get("eventMetadata") or {}
        m0 = ev.get("markets", [{}])[0]
        prices = json.loads(m0.get("outcomePrices", "[0,0]"))
        up_won = float(prices[0]) >= 0.99
        dn_won = float(prices[1]) >= 0.99
        official = "Up" if up_won else ("Down" if dn_won else "?")
        return {
            "price_to_beat": meta.get("priceToBeat"),
            "final_price": meta.get("finalPrice"),
            "closed": bool(m0.get("closed")),
            "official_result": official,
        }
    except Exception as e:
        return {"error": str(e)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "observations.db"), help="含 trades 表的 sqlite")
    ap.add_argument("--mode", default="live", help="筛选 trades.mode")
    args = ap.parse_args()
    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"找不到数据库: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, window_ts, direction, status, pnl, opened_at
        FROM trades
        WHERE mode = ? AND window_ts IS NOT NULL
        ORDER BY opened_at ASC
        """,
        (args.mode,),
    ).fetchall()
    conn.close()

    if not rows:
        print(f"无记录: mode={args.mode} @ {db_path}")
        return 0

    print(
        f"{'id':>5}  {'window_ts':>12}  {'dir':^5}  {'stat':^6}  {'pnl':>8}  "
        f"{'Gamma PtB':>14}  {'final':>14}  {'官方':^6}  note"
    )
    for row in rows:
        wts = int(row["window_ts"])
        g = fetch_gamma(wts)
        if g is None or "error" in g:
            note = g.get("error", "no data") if isinstance(g, dict) else "?"
            ptb_s = fin_s = official = "-"
        else:
            ptb = g.get("price_to_beat")
            fin = g.get("final_price")
            ptb_s = f"{float(ptb):.2f}" if ptb is not None else "—"
            fin_s = f"{float(fin):.2f}" if fin is not None else "—"
            official = g.get("official_result", "?")
            our = row["direction"]
            match = ""
            if official in ("Up", "Down") and our == official:
                match = "方向一致"
            elif official in ("Up", "Down") and our != official:
                match = "方向与官方相反(若当时gap靠边界则可能锚价误差)"
            if ptb is None and fin is None:
                match = "Gamma 尚无 metadata（可能未收盘）"

        pnl = row["pnl"]
        pnl_s = f"{pnl:+.2f}" if pnl is not None else "—"
        print(
            f"{row['id']:5d}  {wts:12d}  {row['direction']:^5}  {row['status']:^6}  {pnl_s:>8}  "
            f"{ptb_s:>14}  {fin_s:>14}  {official:^6}  {match}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
