"""
下单执行模块

支持两种模式：
  paper — 纸面交易（模拟，不消耗资金，用于验证策略）
  live  — 实盘（通过 py-clob-client 在 Polygon 上真实下单）

实盘前置条件：
  1. pip install py-clob-client
  2. 钱包有 USDC.e（交易本金）和 POL（gas）
  3. .env 中配置 PRIVATE_KEY / WALLET_ADDRESS
"""

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import asdict, dataclass
from typing import Optional

from .config import cfg
from .strategy import Direction, Signal

logger = logging.getLogger("executor")


# ─────────────────────────────────────────────
# 交易记录
# ─────────────────────────────────────────────

@dataclass
class TradeRecord:
    id: Optional[int]
    window_ts: int
    direction: str
    token_id: str
    entry_price: float          # 实际成交价
    size: float                 # 份数（USDC = entry_price × size）
    amount_usdc: float          # 投入 USDC
    order_id: Optional[str]     # Polymarket 订单 ID（实盘有，纸面为 None）
    mode: str                   # "paper" | "live"
    status: str                 # "open" | "win" | "loss" | "cancelled"
    opened_at: int
    closed_at: Optional[int]    = None
    pnl: Optional[float]        = None
    theo_win_rate: Optional[float] = None
    ev_per_unit: Optional[float]   = None

    def close(self, result: str, final_price: float):
        """结算：result = 'Up' 或 'Down'"""
        self.closed_at = int(time.time())
        won = (result == self.direction)
        if won:
            profit = self.amount_usdc * (1 - self.entry_price) / self.entry_price
            self.pnl = profit
            self.status = "win"
        else:
            self.pnl = -self.amount_usdc
            self.status = "loss"
        return self.pnl


# ─────────────────────────────────────────────
# 订单数据库
# ─────────────────────────────────────────────

class TradeDB:
    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._create_tables()

    def _create_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                window_ts   INTEGER,
                direction   TEXT,
                token_id    TEXT,
                entry_price REAL,
                size        REAL,
                amount_usdc REAL,
                order_id    TEXT,
                mode        TEXT,
                status      TEXT,
                opened_at   INTEGER,
                closed_at   INTEGER,
                pnl         REAL,
                theo_win_rate REAL,
                ev_per_unit   REAL
            );
            CREATE TABLE IF NOT EXISTS daily_summary (
                date        TEXT PRIMARY KEY,
                trades      INTEGER,
                wins        INTEGER,
                pnl         REAL,
                win_rate    REAL
            );
        """)
        self._conn.commit()

    def insert(self, rec: TradeRecord) -> int:
        cur = self._conn.execute("""
            INSERT INTO trades
            (window_ts, direction, token_id, entry_price, size, amount_usdc,
             order_id, mode, status, opened_at, theo_win_rate, ev_per_unit)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (rec.window_ts, rec.direction, rec.token_id, rec.entry_price,
              rec.size, rec.amount_usdc, rec.order_id, rec.mode,
              rec.status, rec.opened_at, rec.theo_win_rate, rec.ev_per_unit))
        self._conn.commit()
        return cur.lastrowid

    def update_closed(self, trade_id: int, status: str, pnl: float, closed_at: int):
        self._conn.execute("""
            UPDATE trades SET status=?, pnl=?, closed_at=? WHERE id=?
        """, (status, pnl, closed_at, trade_id))
        self._conn.commit()

    def get_open_trades(self) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM trades WHERE status='open' ORDER BY opened_at DESC"
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def stats(self) -> dict:
        row = self._conn.execute("""
            SELECT COUNT(*), SUM(CASE WHEN status='win' THEN 1 ELSE 0 END),
                   SUM(COALESCE(pnl,0))
            FROM trades WHERE status IN ('win','loss')
        """).fetchone()
        total, wins, pnl = row[0] or 0, row[1] or 0, row[2] or 0.0
        return {"total": total, "wins": wins, "pnl": pnl,
                "win_rate": wins / total if total else 0}


# ─────────────────────────────────────────────
# 纸面交易执行器
# ─────────────────────────────────────────────

class PaperExecutor:
    """完全模拟的执行器，不需要钱包也不消耗资金"""

    def __init__(self, db: TradeDB):
        self.db = db

    async def place(self, signal: Signal, window_ts: int) -> Optional[TradeRecord]:
        size = signal.bet_amount / signal.market_price
        rec = TradeRecord(
            id=None,
            window_ts=window_ts,
            direction=signal.direction.value,
            token_id=signal.token_id,
            entry_price=signal.market_price,
            size=size,
            amount_usdc=signal.bet_amount,
            order_id=None,
            mode="paper",
            status="open",
            opened_at=int(time.time()),
            theo_win_rate=signal.theoretical_win_rate,
            ev_per_unit=signal.ev_per_unit,
        )
        rec.id = self.db.insert(rec)
        logger.info(
            "[PAPER] 下单: %s $%.2f @ %.3f  (id=%d)",
            signal.direction.value, signal.bet_amount, signal.market_price, rec.id
        )
        return rec

    async def settle(self, trade: TradeRecord, result: str) -> float:
        pnl = trade.close(result, 0)
        self.db.update_closed(trade.id, trade.status, pnl, trade.closed_at)
        emoji = "✅" if trade.status == "win" else "❌"
        logger.info(
            "[PAPER] 结算 id=%d %s %s  pnl=%+.2f USDC",
            trade.id, emoji, result, pnl
        )
        return pnl


# ─────────────────────────────────────────────
# 实盘执行器
# ─────────────────────────────────────────────

class LiveExecutor:
    """
    真实下单执行器，依赖 py-clob-client。
    只有在 mode=live 且钱包配置完整时才使用。
    """

    def __init__(self, db: TradeDB):
        self.db = db
        self._client = None

    def _get_client(self):
        if self._client:
            return self._client
        try:
            from py_clob_client.client import ClobClient
        except ImportError:
            raise RuntimeError("请先安装: pip install py-clob-client")

        if not cfg.has_wallet:
            raise RuntimeError("实盘模式需要配置 PRIVATE_KEY 和 WALLET_ADDRESS")

        # 派生 L2 API 凭证
        temp = ClobClient(cfg.polymarket_host, key=cfg.private_key, chain_id=cfg.chain_id)
        creds = temp.create_or_derive_api_creds()

        self._client = ClobClient(
            cfg.polymarket_host,
            key=cfg.private_key,
            chain_id=cfg.chain_id,
            creds=creds,
            signature_type=0,
            funder=cfg.wallet_address,
        )
        logger.info("Polymarket CLOB 客户端已初始化 (wallet=%s...)", cfg.wallet_address[:8])
        return self._client

    async def place(self, signal: Signal, window_ts: int) -> Optional[TradeRecord]:
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(None, self._do_place, signal, window_ts)
            return result
        except Exception as e:
            logger.error("下单失败: %s", e)
            return None

    def _do_place(self, signal: Signal, window_ts: int) -> Optional[TradeRecord]:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        client = self._get_client()

        # 获取 tick size
        try:
            mkt_detail = client.get_market(
                # condition_id 需要从 market info 中传入
                # 这里用 token_id 所在市场查询
                signal.token_id[:10] + "..."
            )
            tick_size = str(mkt_detail.get("minimum_tick_size", "0.01"))
            neg_risk = mkt_detail.get("neg_risk", False)
        except Exception:
            tick_size = "0.01"
            neg_risk = False

        size = round(signal.bet_amount / signal.market_price, 2)
        price = round(signal.market_price, 4)

        resp = client.create_and_post_order(
            OrderArgs(
                token_id=signal.token_id,
                price=price,
                size=size,
                side=BUY,
                order_type=OrderType.GTC,
            ),
            options={"tick_size": tick_size, "neg_risk": neg_risk},
        )

        order_id = resp.get("orderID") or resp.get("id", "unknown")
        status_ok = resp.get("status") in ("matched", "live", "delayed")

        if not status_ok:
            logger.warning("订单状态异常: %s", resp)

        rec = TradeRecord(
            id=None,
            window_ts=window_ts,
            direction=signal.direction.value,
            token_id=signal.token_id,
            entry_price=price,
            size=size,
            amount_usdc=signal.bet_amount,
            order_id=order_id,
            mode="live",
            status="open",
            opened_at=int(time.time()),
            theo_win_rate=signal.theoretical_win_rate,
            ev_per_unit=signal.ev_per_unit,
        )
        rec.id = self.db.insert(rec)
        logger.info(
            "[LIVE] 下单成功: %s $%.2f @ %.4f  order_id=%s  (db_id=%d)",
            signal.direction.value, signal.bet_amount, price, order_id, rec.id
        )
        return rec

    async def settle(self, trade: TradeRecord, result: str) -> float:
        pnl = trade.close(result, 0)
        self.db.update_closed(trade.id, trade.status, pnl, trade.closed_at)
        emoji = "✅" if trade.status == "win" else "❌"
        logger.info(
            "[LIVE] 结算 id=%d %s %s  pnl=%+.2f USDC  order_id=%s",
            trade.id, emoji, result, pnl, trade.order_id
        )
        return pnl


# ─────────────────────────────────────────────
# 工厂函数：按 mode 返回对应执行器
# ─────────────────────────────────────────────

def make_executor(mode: str, db: TradeDB):
    if mode == "live":
        logger.info("使用实盘执行器")
        return LiveExecutor(db)
    else:
        logger.info("使用纸面交易执行器（paper mode）")
        return PaperExecutor(db)
