"""
数据源模块

盘中 gap 锚价（无法从 Gamma 实时拿到官方 PtB，见下）：
  1. Polymarket 站点 Next.js 脱水 JSON（crypto-prices 查询）— 与页面展示的 Chainlink 窗口一致，优先使用
  2. Chainlink 链上 BTC/USD 聚合器 (Polygon) — 与 Polymarket 使用的 Data Streams 不同源，但是免费数据中最接近的一类
  3. CryptoCompare 加权 USD / Binance — 作 BN/CC 通道的回退快照

Gamma API 说明：
  eventMetadata.priceToBeat / finalPrice 在多数情况下仅在**市场关闭、结算写入后**才出现在接口响应中，
  因此**不能**依赖其在盘中作为开盘价；解析后仅用于事后校验、日志或分析。

赔率来源：
  CLOB 实时订单簿 midpoint（< 1s 延迟）> Gamma API outcomePrices（仅作备用，最高滞后 0.30+）
"""
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("data_feed")

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"
POLYMARKET_ORIGIN = "https://polymarket.com"

# ── Chainlink BTC/USD 链上聚合器（Polygon PoS） ──
# 更新规则：价格偏差 ≥ 0.5% 或心跳 3600s，实测 BTC 波动时约 15-60s 更新一次
_CL_CONTRACT = "0xc907E116054Ad103354f2D350FD2514433D57F6f"
_CL_SELECTOR = "0xfeaf968c"   # latestRoundData()
_CL_RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://1rpc.io/matic",
    "https://rpc.ankr.com/polygon",
]
_cl_rpc_idx = 0

_TIMEOUT = 8
_SESSION: Optional[requests.Session] = None


def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; polymarket-bot/1.0)",
        "Accept": "application/json",
    })
    retry = Retry(
        total=2, backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    proxy = os.environ.get("PROXY_URL", "").strip()
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


def get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = _build_session()
    return _SESSION


def _get(url: str) -> Optional[dict | list]:
    try:
        resp = get_session().get(url, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.Timeout:
        logger.debug("超时: %s", url[:80])
    except requests.exceptions.RequestException as e:
        logger.debug("HTTP 错误 %s → %s", url[:80], e)
    except Exception as e:
        logger.debug("GET 异常 %s → %s", url[:80], e)
    return None


# ─────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────

@dataclass
class BtcTick:
    price: float
    ts: int


def _parse_event_metadata(ev: dict) -> tuple[Optional[float], Optional[float]]:
    """解析 Gamma 事件级 eventMetadata。字段多在结算后才有，盘中常为缺省。"""
    meta = ev.get("eventMetadata")
    if not meta or not isinstance(meta, dict):
        return None, None

    def _to_float(k: str) -> Optional[float]:
        v = meta.get(k)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return _to_float("priceToBeat"), _to_float("finalPrice")


@dataclass
class MarketInfo:
    """窗口静态信息（每窗口从 Gamma API 获取一次）"""
    window_ts:    int
    condition_id: str
    up_token:     str
    down_token:   str
    # Gamma 初始赔率（仅作备用，已滞后时忽略）
    gamma_up:     float
    gamma_dn:     float
    volume:       float
    active:       bool
    closed:       bool
    fetched_at:   int = field(default_factory=lambda: int(time.time()))
    # 结算后 API 常带 eventMetadata（盘中多为 None）；不作实时 gap 锚价
    price_to_beat: Optional[float] = None
    final_price:   Optional[float] = None
    neg_risk:      bool = False

    # 由 bot 每 poll 更新的实时 CLOB 赔率（初始等于 gamma 值）
    up_odds:  float = 0.5
    down_odds: float = 0.5

    def update_clob_odds(self, up: float, dn: float):
        self.up_odds  = up
        self.down_odds = dn


@dataclass
class OrderBook:
    token_id: str
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]
    fetched_at: int = field(default_factory=lambda: int(time.time()))

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    def ask_depth_at(self, max_price: float) -> float:
        return sum(sz for p, sz in self.asks if p <= max_price)


# ─────────────────────────────────────────────
# BTC 价格
# ─────────────────────────────────────────────

def get_btc_price_rest() -> Optional[float]:
    """Binance BTC/USDT 实时价格（最快，但有系统性偏差约 $20-50）"""
    data = _get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
    if data and "price" in data:
        return float(data["price"])
    return None


def get_cryptocompare_price() -> Optional[float]:
    """CryptoCompare BTC/USD 多交易所加权均价（偏差通常 < $10）"""
    data = _get("https://min-api.cryptocompare.com/data/price?fsym=BTC&tsyms=USD")
    if data and "USD" in data:
        return float(data["USD"])
    return None


def get_chainlink_onchain_price() -> tuple[Optional[float], Optional[int]]:
    """
    从 Polygon 链上读取 Chainlink BTC/USD 聚合器价格。
    返回 (price, oracle_updated_at_timestamp)，失败返回 (None, None)。

    此价格来自链上标准聚合器（每 60s 心跳或 0.5% 偏差触发），
    与 Polymarket 使用的 Data Streams（亚秒级）略有差异，
    但同属 Chainlink DON 网络，是免费数据中最接近真实 PtB 的来源。
    """
    global _cl_rpc_idx
    payload = {
        "jsonrpc": "2.0", "method": "eth_call",
        "params": [{"to": _CL_CONTRACT, "data": _CL_SELECTOR}, "latest"],
        "id": 1,
    }
    for _ in range(len(_CL_RPCS)):
        rpc = _CL_RPCS[_cl_rpc_idx % len(_CL_RPCS)]
        try:
            resp = get_session().post(rpc, json=payload, timeout=_TIMEOUT)
            result = resp.json().get("result", "")
            if result and len(result) >= 130:
                data = result[2:]
                price      = int(data[64:128], 16) / 1e8
                updated_at = int(data[128:192], 16)
                return price, updated_at
        except Exception:
            pass
        _cl_rpc_idx += 1
    return None, None


# ─────────────────────────────────────────────
# Polymarket 市场数据
# ─────────────────────────────────────────────

def get_market_info(window_ts: int) -> Optional[MarketInfo]:
    """
    从 Gamma API 获取窗口静态信息（token_id, condition_id 等）。
    outcomePrices 仅作初始赔率备用；实时赔率请用 get_clob_midpoints()。
    price_to_beat / final_price 来自 eventMetadata，通常仅在**收盘后**才有，勿用于盘中锚定。
    """
    slug = f"btc-updown-5m-{window_ts}"
    data = _get(f"{GAMMA_API}/events?slug={slug}")
    if not data:
        return None
    try:
        ev = data[0]
        m  = ev["markets"][0]
        tokens = json.loads(m["clobTokenIds"])
        prices = json.loads(m["outcomePrices"])
        gamma_up = float(prices[0])
        gamma_dn = float(prices[1])
        ptb, fin = _parse_event_metadata(ev)
        info = MarketInfo(
            window_ts    = window_ts,
            condition_id = m["conditionId"],
            up_token     = tokens[0],
            down_token   = tokens[1],
            gamma_up     = gamma_up,
            gamma_dn     = gamma_dn,
            up_odds      = gamma_up,   # 初始值等于 Gamma（将被 CLOB 覆盖）
            down_odds    = gamma_dn,
            volume       = float(m.get("volume", 0)),
            active       = bool(m.get("active", False)),
            closed       = bool(m.get("closed", False)),
            price_to_beat= ptb,
            final_price  = fin,
            neg_risk     = bool(m.get("negRisk", False)),
        )
        logger.debug(
            "Gamma 静态信息 ts=%d | gamma_up=%.3f gamma_dn=%.3f vol=$%.0f ptb=%s",
            window_ts, gamma_up, gamma_dn, info.volume,
            f"{ptb:.2f}" if ptb is not None else "—",
        )
        return info
    except (KeyError, IndexError, json.JSONDecodeError, ValueError) as e:
        logger.warning("市场数据解析失败 ts=%d: %s", window_ts, e)
        return None


def get_clob_midpoints(
    up_token: str, dn_token: str
) -> tuple[Optional[float], Optional[float]]:
    """
    从 CLOB 订单簿获取 UP/DOWN token 的实时中间价。
    实测更新延迟 < 1s，比 Gamma API 最高快 0.30+ 赔率单位。
    """
    r_up = _get(f"{CLOB_API}/midpoint?token_id={up_token}")
    r_dn = _get(f"{CLOB_API}/midpoint?token_id={dn_token}")
    up_mid = float(r_up["mid"]) if r_up and "mid" in r_up else None
    dn_mid = float(r_dn["mid"]) if r_dn and "mid" in r_dn else None
    return up_mid, dn_mid


def get_orderbook(token_id: str) -> Optional[OrderBook]:
    """获取指定 token 的 CLOB 订单簿"""
    data = _get(f"{CLOB_API}/book?token_id={token_id}")
    if not data:
        return None
    try:
        bids = [(float(b["price"]), float(b["size"])) for b in data.get("bids", [])]
        asks = [(float(a["price"]), float(a["size"])) for a in data.get("asks", [])]
        bids.sort(key=lambda x: -x[0])
        asks.sort(key=lambda x:  x[0])
        return OrderBook(token_id=token_id, bids=bids, asks=asks)
    except (KeyError, ValueError) as e:
        logger.debug("订单簿解析失败: %s", e)
        return None


# ─────────────────────────────────────────────
# Polymarket 网页脱水数据（当前窗口开盘价）
# ─────────────────────────────────────────────

def _iso_utc_z(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_z_to_ts(s: str) -> Optional[int]:
    """将 queryKey 内 ISO 时间（含 ...Z 或 ...000Z）转为 Unix 秒，便于与 window_ts 比较。"""
    if not isinstance(s, str) or not s.strip():
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


def _fetch_text(url: str, timeout: float = 22.0) -> Optional[str]:
    try:
        resp = get_session().get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except requests.exceptions.RequestException as e:
        logger.debug("GET text 失败 %s → %s", url[:100], e)
        return None


def _get_json_timeout(url: str, timeout: float = 16.0) -> Optional[dict | list]:
    try:
        resp = get_session().get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.Timeout:
        logger.debug("超时: %s", url[:80])
    except requests.exceptions.RequestException as e:
        logger.debug("HTTP 错误 %s → %s", url[:80], e)
    except Exception as e:
        logger.debug("GET JSON 异常 %s → %s", url[:80], e)
    return None


def _parse_build_id_from_event_html(html: str) -> Optional[str]:
    """从事件页 __NEXT_DATA__ 解析 Next.js buildId。"""
    marker = 'id="__NEXT_DATA__"'
    start = html.find(marker)
    if start == -1:
        return None
    t0 = html.find(">", start)
    if t0 == -1:
        return None
    t0 += 1
    t1 = html.find("</script>", t0)
    if t1 == -1:
        return None
    try:
        j = json.loads(html[t0:t1])
        bid = j.get("buildId")
        if isinstance(bid, str):
            return bid
    except json.JSONDecodeError as e:
        logger.debug("__NEXT_DATA__ JSON 解析失败: %s", e)
    return None


def _extract_ui_open_price_from_page(page: dict, window_ts: int) -> Optional[float]:
    """
    在 dehydratedState.queries 中查找 queryKey
    ['crypto-prices','price','BTC',<start>,<fiveminute>,<end>] 的 openPrice；
    若无精确匹配，则回退到 closePrice 为 null 的当前根 K 线。
    """
    wt0, wt1 = window_ts, window_ts + 300
    try:
        queries = page["pageProps"]["dehydratedState"]["queries"]
    except (KeyError, TypeError):
        return None
    for q in queries:
        qk = q.get("queryKey")
        if not isinstance(qk, list) or len(qk) < 6:
            continue
        if qk[0] != "crypto-prices" or qk[1] != "price" or qk[2] != "BTC":
            continue
        if qk[4] != "fiveminute":
            continue
        ts_a = _iso_z_to_ts(qk[3])
        ts_b = _iso_z_to_ts(qk[5])
        if ts_a != wt0 or ts_b != wt1:
            continue
        st = q.get("state") or {}
        data = st.get("data")
        if not isinstance(data, dict):
            return None
        op = data.get("openPrice")
        if op is None:
            return None
        try:
            p = float(op)
            return p if p > 0 else None
        except (TypeError, ValueError):
            return None
    for q in queries:
        qk = q.get("queryKey")
        if not isinstance(qk, list) or len(qk) < 6:
            continue
        if qk[0] != "crypto-prices" or qk[1] != "price" or qk[2] != "BTC":
            continue
        if qk[4] != "fiveminute":
            continue
        st = q.get("state") or {}
        data = st.get("data")
        if not isinstance(data, dict):
            continue
        if data.get("closePrice") is not None:
            continue
        op = data.get("openPrice")
        if op is None:
            continue
        try:
            p = float(op)
            if p > 0:
                return p
        except (TypeError, ValueError):
            continue
    return None


def get_polymarket_ui_open_price(window_ts: int) -> Optional[float]:
    """
    从 polymarket.com 事件页对应的 _next/data JSON 读取当前 5m 窗口 BTC 开盘价
   （与前端 crypto-prices 脱水查询一致）。失败返回 None，由调用方回退到 CL/CC/BN。

    依赖未公开页面结构，若站点改版可能失效。
    """
    slug = f"btc-updown-5m-{window_ts}"
    html = _fetch_text(f"{POLYMARKET_ORIGIN}/event/{slug}")
    if not html:
        return None
    build_id = _parse_build_id_from_event_html(html)
    if not build_id:
        logger.debug("未解析到 buildId: %s", slug)
        return None
    for loc in ("en", "zh"):
        url = (
            f"{POLYMARKET_ORIGIN}/_next/data/{build_id}/{loc}/event/{slug}.json"
            f"?slug={slug}"
        )
        page = _get_json_timeout(url)
        if not isinstance(page, dict):
            continue
        price = _extract_ui_open_price_from_page(page, window_ts)
        if price is not None:
            logger.debug("Polymarket 页面开盘价 ts=%d → $%.2f（locale=%s）", window_ts, price, loc)
            return price
    logger.debug("Polymarket 页面开盘价不可用 ts=%d", window_ts)
    return None


# ─────────────────────────────────────────────
# 时间工具
# ─────────────────────────────────────────────

def current_window_ts() -> int:
    now = int(time.time())
    return now - (now % 300)


def seconds_into_window() -> int:
    return int(time.time()) % 300


def seconds_remaining() -> int:
    return 300 - seconds_into_window()


# ─────────────────────────────────────────────
# Binance WebSocket 价格订阅
# ─────────────────────────────────────────────

import asyncio

BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"


class BinancePriceFeed:
    """通过 Binance aggTrade WebSocket 接收实时 BTC 价格。"""

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
            logger.error("请安装: pip install websockets")
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
                    self.latest = BtcTick(price=float(msg["p"]), ts=int(msg["T"]) // 1000)
                except (KeyError, ValueError, json.JSONDecodeError):
                    pass

    def stop(self):
        self._running = False

    @property
    def price(self) -> Optional[float]:
        return self.latest.price if self.latest else None

    async def wait_for_price(self, timeout: float = 10.0) -> Optional[float]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.latest:
                return self.latest.price
            await asyncio.sleep(0.1)
        return None


# ─────────────────────────────────────────────
# 轻量级价格缓存（REST 轮询模式备用）
# ─────────────────────────────────────────────

class PriceCache:
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
