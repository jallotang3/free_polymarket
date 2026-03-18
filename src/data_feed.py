"""
数据源模块
- Binance WebSocket：实时 BTC/USDT 价格（1 秒精度）
- Polymarket Gamma API：市场元数据（token ID、当前赔率）
- Polymarket CLOB API：订单簿深度（买卖价差）
"""
import asyncio
import json
import logging
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("data_feed")

BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
BINANCE_REST = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
# Chainlink BTC/USD 价格（通过 CryptoCompare 聚合多数据源，含 Chainlink）
CHAINLINK_REST = "https://min-api.cryptocompare.com/data/price?fsym=BTC&tsyms=USD"

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; polymarket-bot/1.0)",
    "Accept": "application/json",
}


# ─────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────

@dataclass
class BtcTick:
    price: float
    ts: int  # unix 秒


@dataclass
class MarketInfo:
    window_ts: int          # 窗口开始时间戳
    condition_id: str
    up_token: str
    down_token: str
    up_odds: float          # 最新 Up 赔率（0~1）
    down_odds: float
    volume: float
    active: bool
    closed: bool
    fetched_at: int = field(default_factory=lambda: int(time.time()))


@dataclass
class OrderBook:
    token_id: str
    bids: list[tuple[float, float]]  # [(price, size), ...]
    asks: list[tuple[float, float]]
    fetched_at: int = field(default_factory=lambda: int(time.time()))

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    def ask_depth_at(self, max_price: float) -> float:
        """在 max_price 以内可买到的总量"""
        return sum(sz for p, sz in self.asks if p <= max_price)


# ─────────────────────────────────────────────
# HTTP 工具
# ─────────────────────────────────────────────

def _get(url: str, timeout: int = 8) -> Optional[dict | list]:
    try:
        req = urllib.request.Request(url, headers=_HTTP_HEADERS)
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        logger.warning("HTTP %d %s → %s", e.code, url[:80], e.reason)
        return None
    except urllib.error.URLError as e:
        logger.warning("网络错误 %s → %s", url[:80], e.reason)
        return None
    except Exception as e:
        logger.debug("HTTP GET 异常 %s → %s", url[:80], e)
        return None


# ─────────────────────────────────────────────
# BTC 价格（REST 备用）
# ─────────────────────────────────────────────

def get_btc_price_rest() -> Optional[float]:
    """Binance BTC/USDT 实时价格（毫秒级）"""
    data = _get(BINANCE_REST)
    if data and "price" in data:
        return float(data["price"])
    return None


def get_chainlink_btc_price() -> Optional[float]:
    """
    获取 Chainlink BTC/USD 价格（通过 CryptoCompare 聚合）。
    Chainlink 数据流约每 3-5 秒更新，是 Polymarket 结算的唯一依据。
    与 Binance spot 价格存在差异（通常 < 0.1%，但关键时刻可达 0.2-0.5%）。
    """
    data = _get(CHAINLINK_REST)
    if data and "USD" in data:
        return float(data["USD"])
    return None


# ─────────────────────────────────────────────
# Polymarket 市场数据
# ─────────────────────────────────────────────

def get_market_info(window_ts: int) -> Optional[MarketInfo]:
    """根据窗口时间戳获取市场元数据和赔率"""
    slug = f"btc-updown-5m-{window_ts}"
    data = _get(f"{GAMMA_API}/events?slug={slug}")
    if not data:
        logger.warning("市场数据为空: slug=%s", slug)
        return None
    try:
        ev = data[0]
        m = ev["markets"][0]
        prices = json.loads(m["outcomePrices"])
        tokens = json.loads(m["clobTokenIds"])
        info = MarketInfo(
            window_ts=window_ts,
            condition_id=m["conditionId"],
            up_token=tokens[0],
            down_token=tokens[1],
            up_odds=float(prices[0]),
            down_odds=float(prices[1]),
            volume=float(m.get("volume", 0)),
            active=bool(m.get("active", False)),
            closed=bool(m.get("closed", False)),
        )
        logger.debug(
            "市场数据 ts=%d | Up=%.3f Down=%.3f vol=$%.0f active=%s closed=%s",
            window_ts, info.up_odds, info.down_odds, info.volume, info.active, info.closed,
        )
        return info
    except (KeyError, IndexError, json.JSONDecodeError, ValueError) as e:
        logger.warning("市场数据解析失败 ts=%d: %s", window_ts, e)
        return None


def get_orderbook(token_id: str) -> Optional[OrderBook]:
    """获取指定 token 的 CLOB 订单簿"""
    data = _get(f"{CLOB_API}/book?token_id={token_id}")
    if not data:
        return None
    try:
        bids = [(float(b["price"]), float(b["size"])) for b in data.get("bids", [])]
        asks = [(float(a["price"]), float(a["size"])) for a in data.get("asks", [])]
        bids.sort(key=lambda x: -x[0])  # 降序（最高买价在前）
        asks.sort(key=lambda x: x[0])   # 升序（最低卖价在前）
        return OrderBook(token_id=token_id, bids=bids, asks=asks)
    except (KeyError, ValueError) as e:
        logger.debug("parse orderbook error: %s", e)
        return None


def current_window_ts() -> int:
    """当前5分钟窗口的开始时间戳"""
    now = int(time.time())
    return now - (now % 300)


def seconds_into_window() -> int:
    """当前已进入窗口多少秒（0~299）"""
    now = int(time.time())
    return now % 300


def seconds_remaining() -> int:
    """当前窗口剩余秒数"""
    return 300 - seconds_into_window()


# ─────────────────────────────────────────────
# Binance WebSocket 价格订阅
# ─────────────────────────────────────────────

class BinancePriceFeed:
    """
    通过 Binance aggTrade WebSocket 接收实时 BTC 价格。
    最新价格存储在 self.latest，可随时读取。
    使用方式：
        feed = BinancePriceFeed()
        asyncio.create_task(feed.run())
        # 之后通过 feed.latest 获取最新价格
    """

    def __init__(self):
        self.latest: Optional[BtcTick] = None
        self._running = False
        self._reconnect_delay = 3

    async def run(self):
        self._running = True
        while self._running:
            try:
                await self._connect()
            except Exception as e:
                logger.warning("WebSocket 断开: %s，%ss 后重连", e, self._reconnect_delay)
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60)

    async def _connect(self):
        try:
            import websockets
        except ImportError:
            logger.error("请安装 websockets: pip install websockets")
            raise

        logger.info("连接 Binance WebSocket...")
        async with websockets.connect(BINANCE_WS, ping_interval=20) as ws:
            self._reconnect_delay = 3
            logger.info("Binance WebSocket 已连接")
            async for raw in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw)
                    price = float(msg["p"])
                    ts = int(msg["T"]) // 1000
                    self.latest = BtcTick(price=price, ts=ts)
                except (KeyError, ValueError, json.JSONDecodeError):
                    pass

    def stop(self):
        self._running = False

    @property
    def price(self) -> Optional[float]:
        return self.latest.price if self.latest else None

    async def wait_for_price(self, timeout: float = 10.0) -> Optional[float]:
        """等待直到有价格数据或超时"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.latest:
                return self.latest.price
            await asyncio.sleep(0.1)
        return None


# ─────────────────────────────────────────────
# 轻量级价格缓存（用于 REST 轮询模式）
# ─────────────────────────────────────────────

class PriceCache:
    """REST 轮询模式的 BTC 价格缓存，适合不需要 WebSocket 的场景"""

    def __init__(self, ttl_secs: int = 3):
        self._price: Optional[float] = None
        self._fetched_at: float = 0
        self._ttl = ttl_secs

    def get(self) -> Optional[float]:
        if time.time() - self._fetched_at > self._ttl:
            fresh = get_btc_price_rest()
            if fresh:
                self._price = fresh
                self._fetched_at = time.time()
        return self._price

    async def get_async(self) -> Optional[float]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.get)
