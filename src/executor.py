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
import os
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

    def get_wallet_usdc_balance(self) -> float:
        """Paper 模式无链上余额，返回 0"""
        return 0.0

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

    def sweep_usdc(self, balance: float) -> tuple[bool, float, str]:
        return False, 0.0, "paper 模式不归集"

    def sweep_usdc_exact(self, amount: float) -> tuple[bool, float, str]:
        logger.info("[PAPER] 模拟归集 $%.2f", amount)
        return True, amount, "paper"


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
        self._w3 = None  # web3 实例（懒加载，用于查链上余额）

    def get_wallet_usdc_balance(self) -> float:
        """
        查询钱包链上 USDC.e 余额（实时）。
        失败时返回 0.0，不影响主流程。
        """
        try:
            from web3 import Web3
            if self._w3 is None or not self._w3.is_connected():
                self._w3 = Web3(Web3.HTTPProvider(
                    'https://polygon-bor.publicnode.com',
                    request_kwargs={'timeout': 5},
                ))
            USDC_E = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174'
            ERC20_ABI = [{"name": "balanceOf", "type": "function", "stateMutability": "view",
                          "inputs": [{"name": "account", "type": "address"}],
                          "outputs": [{"name": "", "type": "uint256"}]}]
            usdc = self._w3.eth.contract(
                address=Web3.to_checksum_address(USDC_E), abi=ERC20_ABI
            )
            bal = usdc.functions.balanceOf(
                Web3.to_checksum_address(cfg.wallet_address)
            ).call()
            return bal / 1e6  # USDC.e 6位小数
        except Exception as e:
            logger.debug("查询链上余额失败: %s", e)
            return 0.0

    def sweep_usdc(self, balance: float) -> tuple[bool, float, str]:
        """
        资金归集：余额达到阈值时，转出 阈值 × 比例 到归集钱包。

        计算规则：
            转出金额 = sweep_threshold × sweep_ratio（固定金额）
            例：余额=$120，阈值=$50，比例=0.8 → 转出 50×0.8=$40，保留$80

        参数:
            balance: 当前链上余额（USDC）

        返回:
            (success, amount_sent, tx_hash)
            失败时 success=False, amount_sent=0, tx_hash=错误信息
        """
        if not cfg.has_sweep:
            return False, 0.0, "归集未配置"

        if balance < cfg.sweep_threshold:
            return False, 0.0, f"余额 ${balance:.2f} 未达到阈值 ${cfg.sweep_threshold:.2f}"

        # 转出金额固定为：阈值 × 比例
        amount = round(cfg.sweep_threshold * cfg.sweep_ratio, 2)
        if amount < 1.0:
            return False, 0.0, f"归集金额 ${amount:.2f} 过小（<$1），跳过"

        try:
            from web3 import Web3
        except ImportError:
            return False, 0.0, "web3 未安装"

        try:
            if self._w3 is None or not self._w3.is_connected():
                proxy = os.environ.get("PROXY_URL", "").strip()
                rpc_kwargs = {'timeout': 10}
                if proxy:
                    rpc_kwargs['proxies'] = {'http': proxy, 'https': proxy}
                self._w3 = Web3(Web3.HTTPProvider(
                    'https://polygon-bor.publicnode.com',
                    request_kwargs=rpc_kwargs,
                ))

            USDC_E = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174'
            ERC20_ABI = [
                {"name": "transfer", "type": "function", "stateMutability": "nonpayable",
                 "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
                 "outputs": [{"name": "", "type": "bool"}]},
            ]
            w3 = self._w3
            wallet  = Web3.to_checksum_address(cfg.wallet_address)
            to_addr = Web3.to_checksum_address(cfg.sweep_wallet)
            usdc    = w3.eth.contract(address=Web3.to_checksum_address(USDC_E), abi=ERC20_ABI)
            amount_raw = int(amount * 1e6)  # USDC.e 6位小数

            nonce  = w3.eth.get_transaction_count(wallet)
            gas_px = w3.eth.gas_price
            tx = usdc.functions.transfer(to_addr, amount_raw).build_transaction({
                'from': wallet, 'nonce': nonce,
                'gas': 80_000, 'gasPrice': gas_px, 'chainId': 137,
            })
            signed  = w3.eth.account.sign_transaction(tx, cfg.private_key)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

            if receipt.status == 1:
                logger.info(
                    "💸 资金归集成功: $%.2f USDC → %s (tx: %s)",
                    amount, cfg.sweep_wallet[:10] + "...", tx_hash.hex()[:16] + "...",
                )
                return True, amount, tx_hash.hex()
            else:
                logger.error("❌ 资金归集交易失败 (tx: %s)", tx_hash.hex())
                return False, 0.0, f"交易上链失败: {tx_hash.hex()}"

        except Exception as e:
            logger.error("资金归集异常: %s", e)
            return False, 0.0, str(e)

    def sweep_usdc_exact(self, amount: float) -> tuple[bool, float, str]:
        """
        转出指定金额的 USDC.e 到 SWEEP_WALLET（用于日盈利归集等）。
        不要求达到 sweep_threshold；仅检查余额与归集地址是否配置。
        """
        amount = round(amount, 2)
        if amount < 1.0:
            return False, 0.0, f"归集金额 ${amount:.2f} 过小（<$1）"
        if not cfg.sweep_wallet:
            return False, 0.0, "未配置 SWEEP_WALLET"
        balance = self.get_wallet_usdc_balance()
        if balance < amount - 1e-6:
            return False, 0.0, f"余额不足: ${balance:.2f} < ${amount:.2f}"

        try:
            from web3 import Web3
        except ImportError:
            return False, 0.0, "web3 未安装"

        try:
            if self._w3 is None or not self._w3.is_connected():
                proxy = os.environ.get("PROXY_URL", "").strip()
                rpc_kwargs = {'timeout': 10}
                if proxy:
                    rpc_kwargs['proxies'] = {'http': proxy, 'https': proxy}
                self._w3 = Web3(Web3.HTTPProvider(
                    'https://polygon-bor.publicnode.com',
                    request_kwargs=rpc_kwargs,
                ))

            USDC_E = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174'
            ERC20_ABI = [
                {"name": "transfer", "type": "function", "stateMutability": "nonpayable",
                 "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
                 "outputs": [{"name": "", "type": "bool"}]},
            ]
            w3 = self._w3
            wallet  = Web3.to_checksum_address(cfg.wallet_address)
            to_addr = Web3.to_checksum_address(cfg.sweep_wallet)
            usdc    = w3.eth.contract(address=Web3.to_checksum_address(USDC_E), abi=ERC20_ABI)
            amount_raw = int(amount * 1e6)

            nonce  = w3.eth.get_transaction_count(wallet)
            gas_px = w3.eth.gas_price
            tx = usdc.functions.transfer(to_addr, amount_raw).build_transaction({
                'from': wallet, 'nonce': nonce,
                'gas': 80_000, 'gasPrice': gas_px, 'chainId': 137,
            })
            signed  = w3.eth.account.sign_transaction(tx, cfg.private_key)
            raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
            tx_hash = w3.eth.send_raw_transaction(raw)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

            if receipt.status == 1:
                logger.info(
                    "💸 指定金额归集成功: $%.2f USDC → %s (tx: %s)",
                    amount, cfg.sweep_wallet[:10] + "...", tx_hash.hex()[:16] + "...",
                )
                return True, amount, tx_hash.hex()
            return False, 0.0, f"交易上链失败: {tx_hash.hex()}"
        except Exception as e:
            logger.error("指定金额归集异常: %s", e)
            return False, 0.0, str(e)

    def _get_client(self):
        if self._client:
            return self._client
        try:
            from py_clob_client.client import ClobClient
        except ImportError:
            raise RuntimeError("请先安装: pip install py-clob-client")

        if not cfg.has_wallet:
            raise RuntimeError("实盘模式需要配置 PRIVATE_KEY 和 WALLET_ADDRESS")

        # py-clob-client 内部使用 httpx，通过环境变量注入代理
        # 必须在 ClobClient 初始化前设置，否则 httpx 不会读取
        proxy = os.environ.get("PROXY_URL", "").strip()
        if proxy:
            os.environ["HTTPS_PROXY"] = proxy
            os.environ["HTTP_PROXY"]  = proxy
            logger.debug("httpx 代理已注入: %s", proxy)

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

        # 自动检查并补全 USDC.e approve（仅首次初始化时执行）
        self._ensure_usdc_allowance()
        return self._client

    def _ensure_usdc_allowance(self):
        """
        检查钱包对 Polymarket 三个合约的 USDC.e allowance。
        若 allowance 不足，自动发送链上 approve 交易（无限额度）。
        Polymarket 合约地址（Polygon）：
          CTF Exchange:      0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E
          Neg Risk Exchange: 0xC5d563A36AE78145C45a50134d48A1215220f80a
          Neg Risk Adapter:  0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296
        """
        try:
            from web3 import Web3
        except ImportError:
            logger.warning("web3 未安装，跳过 allowance 检查。如遇 'not enough allowance' 错误，请运行: pip install web3")
            return

        USDC_E = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174'
        SPENDERS = [
            ('CTF Exchange',      '0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E'),
            ('Neg Risk Exchange', '0xC5d563A36AE78145C45a50134d48A1215220f80a'),
            ('Neg Risk Adapter',  '0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296'),
        ]
        POLYGON_RPC = 'https://polygon-bor.publicnode.com'
        MIN_ALLOWANCE = 10 * 10**6  # 低于 10 USDC.e 视为需补充授权

        ERC20_ABI = [
            {"name": "allowance", "type": "function", "stateMutability": "view",
             "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
             "outputs": [{"name": "", "type": "uint256"}]},
            {"name": "approve", "type": "function", "stateMutability": "nonpayable",
             "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
             "outputs": [{"name": "", "type": "bool"}]},
        ]

        try:
            proxy = os.environ.get("PROXY_URL", "").strip()
            rpc_kwargs = {'timeout': 10}
            if proxy:
                rpc_kwargs['proxies'] = {'http': proxy, 'https': proxy}
            w3 = Web3(Web3.HTTPProvider(POLYGON_RPC, request_kwargs=rpc_kwargs))
            if not w3.is_connected():
                logger.warning("Polygon RPC 连接失败，跳过 allowance 检查")
                return

            wallet = Web3.to_checksum_address(cfg.wallet_address)
            usdc   = w3.eth.contract(address=Web3.to_checksum_address(USDC_E), abi=ERC20_ABI)
            MAX_UINT = 2**256 - 1

            needs_approve = []
            for name, spender in SPENDERS:
                al = usdc.functions.allowance(wallet, Web3.to_checksum_address(spender)).call()
                if al < MIN_ALLOWANCE:
                    needs_approve.append((name, spender))
                    logger.info("USDC.e allowance 不足 [%s]: %d，需要 approve", name, al)

            if not needs_approve:
                logger.info("✅ USDC.e allowance 检查通过，所有合约已授权")
                return

            # 发送 approve 交易
            for name, spender in needs_approve:
                nonce    = w3.eth.get_transaction_count(wallet)
                gas_px   = w3.eth.gas_price
                tx = usdc.functions.approve(
                    Web3.to_checksum_address(spender), MAX_UINT
                ).build_transaction({
                    'from': wallet, 'nonce': nonce,
                    'gas': 100_000, 'gasPrice': gas_px, 'chainId': 137,
                })
                signed   = w3.eth.account.sign_transaction(tx, cfg.private_key)
                tx_hash  = w3.eth.send_raw_transaction(signed.raw_transaction)
                logger.info("USDC.e approve [%s] tx: %s", name, tx_hash.hex())
                receipt  = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                if receipt.status == 1:
                    logger.info("✅ approve [%s] 上链成功 (区块 %d)", name, receipt.blockNumber)
                else:
                    logger.error("❌ approve [%s] 上链失败", name)

        except Exception as e:
            logger.warning("allowance 检查/授权异常（不影响下单，若持续报错请手动 approve）: %s", e)

    async def place(self, signal: Signal, window_ts: int) -> Optional[TradeRecord]:
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(None, self._do_place, signal, window_ts)
            return result
        except Exception as e:
            err = str(e)
            # 区分网络错误 vs API 拒绝，方便排查
            if "Request exception" in err or "timeout" in err.lower() or "connection" in err.lower():
                logger.error("下单失败（网络错误，已重试%d次）: %s", 3, err)
            else:
                logger.error("下单失败: %s", e)
            return None

    def _do_place(self, signal: Signal, window_ts: int) -> Optional[TradeRecord]:
        from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions
        from py_clob_client.order_builder.constants import BUY

        client = self._get_client()

        # 获取 tick size（通过 condition_id 查询）
        tick_size = "0.01"
        neg_risk  = False
        try:
            mkt_detail = client.get_market(signal.condition_id)
            tick_size  = str(mkt_detail.get("minimum_tick_size", "0.01"))
            neg_risk   = bool(mkt_detail.get("neg_risk", False))
        except Exception as e:
            logger.debug("get_market 失败，使用默认 tick_size=0.01: %s", e)

        MIN_SIZE = 5.0  # Polymarket 最小订单 size（token 数量）
        size = round(signal.bet_amount / signal.market_price, 2)
        if size < MIN_SIZE:
            # 补足到最小 size，同时重新计算实际 USDC 花费
            size = MIN_SIZE
            logger.info("订单 size %.2f 低于最小值 %g，调整到 %g token（实际花费 $%.2f）",
                        signal.bet_amount / signal.market_price, MIN_SIZE, size,
                        size * signal.market_price)
        price = round(signal.market_price, 4)

        # 下单重试（最多3次，指数退避）
        # 针对 "Request exception!" 这类网络抖动导致的临时失败
        MAX_ATTEMPTS = 3
        resp = None
        last_exc = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = client.create_and_post_order(
                    OrderArgs(
                        token_id=signal.token_id,
                        price=price,
                        size=size,
                        side=BUY,
                    ),
                    options=PartialCreateOrderOptions(
                        tick_size=tick_size,
                        neg_risk=neg_risk,
                    ),
                )
                last_exc = None
                break  # 成功，跳出重试循环
            except Exception as exc:
                last_exc = exc
                err_msg = str(exc)
                # 仅对网络层错误重试；API 明确拒绝（余额不足/签名错误）不重试
                api_rejection = any(k in err_msg for k in [
                    "not enough balance", "invalid signature",
                    "order is invalid", "minimum",
                ])
                if api_rejection or attempt == MAX_ATTEMPTS:
                    raise
                wait = 1.5 ** attempt  # 1.5s → 2.25s
                logger.warning("下单第%d次失败（%.1fs后重试）: %s", attempt, wait, err_msg[:80])
                time.sleep(wait)

        if last_exc is not None:
            raise last_exc

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
            amount_usdc=round(size * price, 4),  # 实际花费（已修正 min size 后）
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
