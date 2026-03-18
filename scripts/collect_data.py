"""
Phase 1 数据采集脚本 — 无需钱包，只读，免费
功能：每 5 秒采集一次当前窗口的 BTC 价格 + Polymarket Up/Down 赔率
目的：验证在第 4 分钟 gap ≥ 0.10% 时，市场赔率是否低于 0.93（套利机会窗口）

代理支持（可选）：
  在 .env 中设置 PROXY_URL，格式：
    http://127.0.0.1:7890        # HTTP 代理
    https://127.0.0.1:7890       # HTTPS 代理
    socks5://127.0.0.1:1080      # SOCKS5 代理（需安装 PySocks: pip install PySocks）
    socks5h://127.0.0.1:1080     # SOCKS5 代理（DNS 也走代理）
  留空则直连。
"""

import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 加载 .env 文件（优先从项目根目录读取）
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(_env_path)
except ImportError:
    pass  # python-dotenv 未安装时跳过，依赖系统环境变量

# ────────────────────────────
DB_PATH = "observations.db"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
POLL_INTERVAL = 5  # 秒
TIMEOUT = 8        # HTTP 请求超时秒数
# ────────────────────────────


def _build_session() -> requests.Session:
    """
    构建全局 HTTP Session，支持代理和自动重试。

    代理优先级：
      1. 环境变量 PROXY_URL（在 .env 中设置）
      2. 系统环境变量 HTTPS_PROXY / HTTP_PROXY（自动继承）
      3. 不设置则直连
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    # 重试策略：网络错误时自动重试 2 次
    retry = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    # 读取代理配置（支持 http/https/socks5/socks5h）
    proxy_url = os.environ.get("PROXY_URL", "").strip()
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}
        print(f"  [代理] 已启用代理: {proxy_url}")
    else:
        # 检查是否有系统级代理环境变量
        sys_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        if sys_proxy:
            print(f"  [代理] 检测到系统代理: {sys_proxy}")
        else:
            print("  [代理] 直连模式（如需代理，在 .env 中设置 PROXY_URL）")

    return session


# 全局 session，整个脚本生命周期复用
_SESSION: requests.Session | None = None


def get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = _build_session()
    return _SESSION


def init_db():
    conn = sqlite3.connect(DB_PATH)
    # 建表（首次运行）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS observations (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ts               INTEGER NOT NULL,
            window_ts        INTEGER NOT NULL,
            minute_in_window INTEGER NOT NULL,
            btc_price        REAL,
            chainlink_price  REAL,
            price_to_beat    REAL,
            cl_price_to_beat REAL,
            gap_pct          REAL,
            cl_gap_pct       REAL,
            price_divergence REAL,
            up_odds          REAL,
            down_odds        REAL,
            volume           REAL,
            condition_id     TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS window_results (
            window_ts   INTEGER PRIMARY KEY,
            result      TEXT,
            final_btc   REAL,
            recorded_at INTEGER
        )
    """)
    # 迁移：为旧表补充新列（ALTER TABLE 对已存在的列会报错，忽略即可）
    new_columns = [
        ("chainlink_price",  "REAL"),
        ("cl_price_to_beat", "REAL"),
        ("cl_gap_pct",       "REAL"),
        ("price_divergence", "REAL"),
    ]
    existing = {row[1] for row in conn.execute("PRAGMA table_info(observations)")}
    for col_name, col_type in new_columns:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE observations ADD COLUMN {col_name} {col_type}")
            print(f"  [DB 迁移] 已添加列: {col_name}")
    conn.commit()
    return conn


def fetch_json(url: str) -> dict | list | None:
    """通过全局 session（支持代理）发起 GET 请求并解析 JSON。"""
    try:
        resp = get_session().get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.ProxyError as e:
        print(f"  [代理错误] {url[:60]}... → {e}")
        return None
    except requests.exceptions.SSLError as e:
        print(f"  [SSL错误] {url[:60]}... → {e}")
        return None
    except requests.exceptions.Timeout:
        print(f"  [超时] {url[:60]}...")
        return None
    except requests.exceptions.ConnectionError as e:
        print(f"  [连接错误] {url[:60]}... → {e}")
        return None
    except Exception as e:
        print(f"  [HTTP error] {url[:60]}... → {e}")
        return None


def get_btc_price() -> float | None:
    """从 Binance 获取 BTC/USDT 最新价格"""
    data = fetch_json("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
    if data:
        return float(data["price"])
    return None


def get_cryptocompare_price() -> float | None:
    """
    从 CryptoCompare 获取 BTC/USD 聚合价格。

    ⚠️  注意：这不是 Chainlink 价格！
    Polymarket 使用的是 Chainlink Data Streams（需付费 API key），
    CryptoCompare 是多交易所加权均值，与真实 Chainlink PtB 通常差 $20–60。
    本字段仅用于辅助参考，策略核心应依赖市场赔率跳变信号。
    """
    data = fetch_json("https://min-api.cryptocompare.com/data/price?fsym=BTC&tsyms=USD")
    if data and "USD" in data:
        return float(data["USD"])
    return None


def get_polymarket_window(window_ts: int) -> dict | None:
    """获取指定时间戳的 BTC 5m 窗口市场数据"""
    slug = f"btc-updown-5m-{window_ts}"
    data = fetch_json(f"https://gamma-api.polymarket.com/events?slug={slug}")
    if not data:
        return None
    try:
        ev = data[0]
        m = ev["markets"][0]
        prices = json.loads(m["outcomePrices"])
        tokens = json.loads(m["clobTokenIds"])
        return {
            "condition_id": m["conditionId"],
            "up_token": tokens[0],
            "down_token": tokens[1],
            "up_odds": float(prices[0]),
            "down_odds": float(prices[1]),
            "volume": float(m.get("volume", 0)),
            "active": m.get("active", False),
            "closed": m.get("closed", False),
        }
    except (KeyError, IndexError, json.JSONDecodeError):
        return None


def get_current_window_ts() -> int:
    """当前5分钟窗口的开始时间戳（对齐到 ts % 300 == 0）"""
    now = int(time.time())
    return now - (now % 300)


def current_minute_in_window(window_ts: int) -> int:
    """当前处于窗口第几分钟（0-4）"""
    now = int(time.time())
    return (now - window_ts) // 60


def log_observation(conn: sqlite3.Connection, obs: dict):
    conn.execute("""
        INSERT INTO observations
        (ts, window_ts, minute_in_window,
         btc_price, chainlink_price, price_to_beat, cl_price_to_beat,
         gap_pct, cl_gap_pct, price_divergence,
         up_odds, down_odds, volume, condition_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        obs["ts"], obs["window_ts"], obs["minute_in_window"],
        obs["btc_price"], obs.get("chainlink_price"), obs["price_to_beat"],
        obs.get("cl_price_to_beat"), obs["gap_pct"], obs.get("cl_gap_pct"),
        obs.get("price_divergence"),
        obs["up_odds"], obs["down_odds"], obs["volume"], obs["condition_id"],
    ))
    conn.commit()


def analyze_opportunity(
    obs: dict,
    btc_trend_pct: float | None = None,
    bn_gap_delta: float | None = None,
    ptb_delay_secs: int = 0,
) -> str:
    """
    实时判断当前是否有套利机会。

    btc_trend_pct  : 本次 poll BTC 价格相对上次的变化（%），正=涨，负=跌
    bn_gap_delta   : Binance gap 相对上次 poll 的变化（%）
    ptb_delay_secs : PtB 记录时距窗口开盘已过去的秒数（越大越不可靠）
    """
    gap = obs["gap_pct"]           # CC 估算 gap（以 CC PtB 为基准）
    cl_gap = obs.get("cl_gap_pct")
    minute = obs["minute_in_window"]
    up_odds = obs["up_odds"]
    down_odds = obs["down_odds"]

    # ── PtB 延迟过大：信号不可靠 ──
    # PtB 超过 60s 延迟时，基准价已经是分1之后的价格，gap 方向无参考价值
    if ptb_delay_secs > 60:
        return (f"⏳ PtB延迟{ptb_delay_secs}s，基准失真，忽略gap信号。"
                f"仅赔率跳变信号可信。")

    # ── 市场方向软冲突检查 ──
    # 区分两种情况：
    #   A. 赔率发生了「跳变」（如从0.50→0.65）→ Chainlink 在本窗口确认了反向，冲突可信
    #   B. 赔率从开盘就静止在某个偏置值（如一直是0.54）→ 可能是惯性/做市商报价，不代表 Chainlink 确认
    #
    # 历史教训：01:40 窗口 Down=0.54 全程静止，软冲突阻止了正确的 UP 信号，最终 UP 胜出。
    # 因此：静止偏置（无跳变）时，阈值提高到 0.65；有跳变确认时，阈值降低到 0.58。
    if abs(gap) >= 0.05 and minute >= 2:
        signal_up = gap > 0
        opposing_odds = up_odds if not signal_up else down_odds
        # 判断对立方赔率是否经历过跳变（由调用方传入 odds_jumped 标志）
        # 无跳变时用更高阈值（0.65），有跳变时用更低阈值（0.58）
        soft_threshold = 0.58 if obs.get("_odds_jumped", False) else 0.65
        if opposing_odds > soft_threshold:
            signal_dir = "UP" if signal_up else "DOWN"
            opp_dir    = "DOWN" if signal_up else "UP"
            jump_note  = "(赔率已跳变，Chainlink确认)" if obs.get("_odds_jumped") else "(静止偏置，参考价值有限)"
            return (f"⚠️ 市场软冲突: 估算gap→{signal_dir} 但赔率{opp_dir}={opposing_odds:.2f}"
                    f">{soft_threshold}{jump_note}，不操作")

    # 方向冲突：Binance 和 CryptoCompare 估算方向相反
    if cl_gap is not None and abs(gap) >= 0.05 and abs(cl_gap) >= 0.02:
        if (gap > 0) != (cl_gap > 0):
            return f"⚠️ 估算方向冲突 Binance={'UP' if gap>0 else 'DN'} CC={'UP' if cl_gap>0 else 'DN'}，不操作"

    # 市场方向强冲突：赔率 > 0.60 明确指向反向，以赔率为准
    # 注意：只有赔率 > 0.72 的信号才给 🟢（0.62 这类中间值太不稳定，可在分4逆转到0.06）
    if abs(gap) >= 0.05 and minute >= 3:
        binance_up = gap > 0
        market_up_dominant = up_odds > 0.60
        market_dn_dominant = down_odds > 0.60
        if (binance_up and market_dn_dominant) or (not binance_up and market_up_dominant):
            market_dir = "UP" if market_up_dominant else "DOWN"
            market_price = up_odds if market_up_dominant else down_odds

            # ── 趋势冲突检查 ──
            trend_opposes_market = False
            trend_note = ""
            if btc_trend_pct is not None and abs(btc_trend_pct) > 0.01:
                btc_rising = btc_trend_pct > 0
                if (market_dir == "DOWN" and btc_rising) or (market_dir == "UP" and not btc_rising):
                    trend_opposes_market = True
                    trend_note = f"BTC趋势{btc_trend_pct:+.3f}%/poll反向"

            # BN gap 方向与赔率相反且幅度 > 0.08%（阈值从 0.10 降到 0.08，防止边界漏网）
            BN_COUNTER_THRESHOLD = 0.08
            bn_large_counter = False
            if (market_dir == "DOWN" and gap > BN_COUNTER_THRESHOLD) or \
               (market_dir == "UP" and gap < -BN_COUNTER_THRESHOLD):
                bn_large_counter = True
                trend_note = (trend_note + f" BN-gap反向幅度{gap:+.3f}%>{BN_COUNTER_THRESHOLD:.2f}%").strip()

            if trend_opposes_market or bn_large_counter:
                return (f"⚠️ 赔率存疑! 方向={market_dir}(市场赔率={market_price:.2f}) "
                        f"但{trend_note}，gap可能已回穿Chainlink PtB，勿操作")

            # ── 赔率强度检查 ──
            # 赔率 0.60-0.72 属于"中等确信"，历史上可在分4彻底逆转（如本窗口 0.62→0.06）
            # 只有 >= 0.72 才认为 Chainlink 已足够稳定确认方向，给出 🟢 信号
            STRONG_ODDS_THRESHOLD = 0.72
            if market_price < STRONG_ODDS_THRESHOLD:
                return (f"⚪ 赔率不够强: 方向={market_dir} 市场={market_price:.2f}"
                        f"<{STRONG_ODDS_THRESHOLD}，中等确信度易逆转，不操作")

            # 无冲突、赔率够强：给出跟赔率信号
            theo_wr = 0.897 if market_price < 0.85 else 0.968
            ev = theo_wr * (1 - market_price) - (1 - theo_wr) * market_price
            if ev > 0.05:
                return (f"🟢 跟赔率! 方向={market_dir}(赔率主导) 理论≈{theo_wr:.1%} "
                        f"市场={market_price:.2f} EV={ev:+.3f}  [Chainlink赔率强确认]")
            else:
                return (f"⚪ 跟赔率EV不足 方向={market_dir} 市场={market_price:.2f} EV={ev:+.3f}"
                        f"  [估算gap反向，Chainlink赔率优先]")

    # 理论胜率查表（基于历史回测，gap 越大胜率越高）
    def theoretical_win_rate(gap_abs: float, min_in_window: int) -> float:
        if min_in_window < 3:
            return 0.5  # 前3分钟不可靠
        if gap_abs >= 0.30:
            return 0.995
        elif gap_abs >= 0.20:
            return 0.982
        elif gap_abs >= 0.15:
            return 0.979
        elif gap_abs >= 0.10:
            return 0.968
        elif gap_abs >= 0.05:
            return 0.897
        else:
            return 0.5

    gap_abs = abs(gap)
    theo_wr = theoretical_win_rate(gap_abs, minute)

    if gap > 0:
        direction = "UP"
        market_price = up_odds
    else:
        direction = "DOWN"
        market_price = down_odds

    if theo_wr < 0.85:
        return "⚪ 无信号"

    ev = theo_wr * (1 - market_price) - (1 - theo_wr) * market_price

    # Polymarket 手续费（加密货币市场动态费率）：
    #   fee = 0.25 × (p × (1-p))²，最高 1.56%（赔率0.50时），越极端越低
    # 手续费 EV 影响：fee_ev = theo_wr × fee_frac（赔出份额被扣减）
    # EV > 0.05 时，手续费最多消耗 0.02，净 EV 仍远为正
    fee_frac = 0.25 * (market_price * (1 - market_price)) ** 2
    ev_after_fee = ev - theo_wr * fee_frac  # 保守估算含手续费 EV

    if ev > 0.05:
        return (f"🟢 强套利! 方向={direction} 理论={theo_wr:.1%} 市场={market_price:.2f} "
                f"EV={ev:+.3f}(含费≈{ev_after_fee:+.3f})")
    elif ev > 0.02:
        return (f"🟡 弱套利  方向={direction} 理论={theo_wr:.1%} 市场={market_price:.2f} "
                f"EV={ev:+.3f}(含费≈{ev_after_fee:+.3f})")
    elif ev > 0:
        return (f"⚪ 微弱    方向={direction} 理论={theo_wr:.1%} 市场={market_price:.2f} "
                f"EV={ev:+.3f}(含费≈{ev_after_fee:+.3f})")
    else:
        return (f"🔴 无利润  方向={direction} 理论={theo_wr:.1%} 市场={market_price:.2f} "
                f"EV={ev:+.3f} (market efficient)")


async def main():
    print("=" * 60)
    print("Polymarket BTC 5m 数据采集器 — Phase 1 验证")
    print("数据存储到 observations.db，按 Ctrl+C 停止")
    print("=" * 60)

    conn = init_db()
    window_cache: dict[int, dict] = {}   # 缓存已获取的窗口数据
    price_to_beat_cache: dict[int, float] = {}  # 记录每个窗口的开盘价（Binance）
    cc_ptb_cache: dict[int, float] = {}          # CryptoCompare 开盘估算价（非 Chainlink）
    prev_up_odds: float | None = None            # 上一次赔率，用于检测赔率跳变
    prev_window_ts: int | None = None            # 上一次窗口 ts，用于检测窗口切换
    prev_btc_price: float | None = None          # 上一次 BTC 价格，用于计算价格趋势
    prev_bn_gap: float | None = None             # 上一次 Binance gap，用于计算 gap 变化趋势
    window_odds_jumped: bool = False             # 本窗口是否发生过赔率跳变（≥0.10）

    try:
        while True:
            now = int(time.time())
            window_ts = get_current_window_ts()
            seconds_elapsed = now - window_ts   # 当前窗口已经过去的秒数
            minute = seconds_elapsed // 60
            ts_str = datetime.now(timezone.utc).strftime("%H:%M:%S")

            # 获取 Binance 和 CryptoCompare 价格（后者仅为参考，非真实 Chainlink）
            btc_price = get_btc_price()
            chainlink_price = get_cryptocompare_price()  # 实际是 CryptoCompare，变量名保留供数据库兼容

            if btc_price is None:
                print(f"[{ts_str}] ⚠️  无法获取 BTC 价格，跳过")
                await asyncio.sleep(POLL_INTERVAL)
                continue

            # Binance vs CryptoCompare 价格偏差（注意：CC ≠ Chainlink，差值无法代表真实偏差）
            price_divergence = None
            if chainlink_price:
                price_divergence = (btc_price - chainlink_price) / chainlink_price * 100

            # ── 新窗口切换：重置跨窗口状态 ──
            if window_ts != prev_window_ts:
                prev_up_odds = None    # 清空，否则会把上一窗口的赔率误判为跳变
                prev_btc_price = None  # 清空，避免跨窗口的价格趋势计算
                prev_bn_gap = None
                window_odds_jumped = False  # 新窗口重置跳变标志
                prev_window_ts = window_ts

            # ── 记录窗口开盘估算价（非 Polymarket 真实 PtB）──
            # ⚠️  Polymarket 使用 Chainlink Data Streams（付费），我们只能用 Binance/CryptoCompare 估算。
            #     估算值与 Polymarket 真实 PtB 通常有 $20–130 的差距（不稳定）。
            #     策略中真正可靠的信号是「赔率跳变」，而不是本地计算的 gap。
            if window_ts not in price_to_beat_cache:
                ptb_price = chainlink_price or btc_price
                price_to_beat_cache[window_ts] = btc_price   # Binance 估算（备用）
                cc_ptb_cache[window_ts] = ptb_price          # CryptoCompare 估算（主用，但非 Chainlink）
                cc_ptb_cache[f"delay_{window_ts}"] = seconds_elapsed  # 记录 PtB 延迟秒数

                if seconds_elapsed <= POLL_INTERVAL:
                    quality = "精确"
                elif seconds_elapsed <= 30:
                    quality = f"轻微延迟({seconds_elapsed}s)"
                else:
                    quality = f"⚠️ 延迟较大({seconds_elapsed}s)"

                # 明确标注这是估算值，不是 Chainlink 真实 PtB
                ptb_src = "CC估算PtB" if chainlink_price else "Binance估算PtB"
                print(f"[{ts_str}] 📌 新窗口开盘  {ptb_src}=${ptb_price:,.2f}  [{quality}]"
                      f"  ⚠️ 真实Chainlink PtB以Polymarket页面为准")

            price_to_beat = price_to_beat_cache.get(window_ts, btc_price)   # Binance 估算 PtB
            cl_ptb = cc_ptb_cache.get(window_ts)                             # CryptoCompare 估算 PtB（非 Chainlink）
            # gap_pct：CC估算价 vs CC估算PtB（仅供参考，非 Polymarket 真实 gap）
            # 与 Polymarket 真实 gap 可能有 ±0.1% 偏差 → 赔率跳变才是更可靠的入场信号
            if chainlink_price and cl_ptb:
                gap_pct = (chainlink_price - cl_ptb) / cl_ptb * 100        # ← 主用
                cl_gap_pct = gap_pct
                binance_gap_pct = (btc_price - price_to_beat) / price_to_beat * 100
            else:
                gap_pct = (btc_price - price_to_beat) / price_to_beat * 100
                cl_gap_pct = None
                binance_gap_pct = gap_pct

            # 获取 Polymarket 赔率（每分钟刷新一次，避免频繁请求）
            cache_key = f"{window_ts}_{minute}"
            if cache_key not in window_cache:
                mkt = get_polymarket_window(window_ts)
                if mkt:
                    window_cache[cache_key] = mkt
                else:
                    print(f"[{ts_str}] ⚠️  无法获取市场数据")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

            mkt = window_cache.get(cache_key)
            if not mkt:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            obs = {
                "ts": now,
                "window_ts": window_ts,
                "minute_in_window": minute,
                "btc_price": btc_price,
                "chainlink_price": chainlink_price,
                "price_to_beat": price_to_beat,
                "cl_price_to_beat": cl_ptb,
                "gap_pct": gap_pct,
                "cl_gap_pct": cl_gap_pct,
                "price_divergence": price_divergence,
                "up_odds": mkt["up_odds"],
                "down_odds": mkt["down_odds"],
                "volume": mkt["volume"],
                "condition_id": mkt["condition_id"],
            }

            # 写入数据库
            log_observation(conn, obs)

            # ── 价格趋势计算（当前 poll 与上一 poll 的 Binance 价格变化方向）──
            btc_trend_pct: float | None = None
            bn_gap_delta: float | None = None
            if prev_btc_price is not None and btc_price is not None:
                btc_trend_pct = (btc_price - prev_btc_price) / prev_btc_price * 100
            if prev_bn_gap is not None:
                bn_gap_delta = binance_gap_pct - prev_bn_gap  # 正值=gap 在扩大（往信号方向走）
            prev_btc_price = btc_price
            prev_bn_gap = binance_gap_pct

            # ── 赔率跳变检测（最可靠信号，不依赖 PtB）──
            odds_jump_signal = ""
            if prev_up_odds is not None:
                delta = mkt["up_odds"] - prev_up_odds
                if abs(delta) >= 0.10:
                    jump_dir = "UP" if delta > 0 else "DOWN"
                    dominant = mkt["up_odds"] if delta > 0 else mkt["down_odds"]
                    odds_jump_signal = (
                        f" 🔔 赔率跳变{delta:+.2f}→{jump_dir}={dominant:.2f}"
                        f"(Chainlink已确认)"
                    )
                    window_odds_jumped = True   # 标记本窗口发生过跳变
            prev_up_odds = mkt["up_odds"]

            # ── 赔率静止警告（赔率长时间不动可能表示市场流动性低或赔率已锁定）──
            # 通过 window_cache 中同一 window 的所有赔率值来判断
            window_odds_list = [
                v["up_odds"] for k, v in window_cache.items()
                if k.startswith(str(window_ts))
            ]
            odds_static_warn = ""
            if len(window_odds_list) >= 4:
                spread = max(window_odds_list) - min(window_odds_list)
                if spread < 0.02:  # 4+ 分钟内赔率几乎不动
                    odds_static_warn = " 📊[赔率静止，流动性可能极低，信号可靠性下降]"

            # 方向冲突检测（Binance 与 CryptoCompare 方向相反时警告）
            conflict = ""
            if cl_gap_pct is not None and abs(gap_pct) >= 0.05:
                if (gap_pct > 0) != (cl_gap_pct > 0):
                    conflict = f" ⚠️CONFLICT(CC={cl_gap_pct:+.3f}%)"

            # 记录本窗口的 PtB 延迟，传入信号分析（延迟>60s时信号降级）
            ptb_delay = cc_ptb_cache.get(f"delay_{window_ts}", 0)

            # 把「本窗口是否有赔率跳变」注入 obs，供软冲突检查区分「跳变确认」vs「静止偏置」
            obs["_odds_jumped"] = window_odds_jumped

            # 实时分析（传入趋势数据和 PtB 延迟）
            signal = analyze_opportunity(
                obs,
                btc_trend_pct=btc_trend_pct,
                bn_gap_delta=bn_gap_delta,
                ptb_delay_secs=ptb_delay,
            )

            # 输出格式：显示CC估算gap（非真实Chainlink gap），括号标注CC/BN来源
            cl_arrow = "↑" if gap_pct > 0 else ("↓" if gap_pct < 0 else "─")
            if chainlink_price and abs(gap_pct - binance_gap_pct) > 0.02:
                gap_str = f"{cl_arrow}{gap_pct:+.3f}%(CC估算) / {binance_gap_pct:+.3f}%(BN)"
            else:
                gap_str = f"{cl_arrow}{gap_pct:+.3f}%(估算)"

            print(
                f"[{ts_str}] 分{minute} | "
                f"BTC=${btc_price:,.1f} {gap_str} | "
                f"Up={mkt['up_odds']:.2f} Down={mkt['down_odds']:.2f} | "
                f"{signal}{conflict}{odds_jump_signal}{odds_static_warn}"
            )

            await asyncio.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\n\n采集停止。生成统计报告...")
        _print_summary(conn)
    finally:
        conn.close()


def _print_summary(conn: sqlite3.Connection):
    """打印采集数据的统计摘要"""
    rows = conn.execute("""
        SELECT minute_in_window, gap_pct, up_odds, down_odds
        FROM observations
        WHERE ABS(gap_pct) >= 0.10 AND minute_in_window >= 3
    """).fetchall()

    if not rows:
        print("数据不足，请采集更多数据后再分析。")
        return

    print(f"\n总计 {len(rows)} 条 gap≥0.10% 的末期观察：")
    discounts = []
    for row in rows:
        minute, gap, up_odds, down_odds = row
        if gap > 0:
            discount = 1.0 - up_odds   # 距离1的距离
            market_price = up_odds
        else:
            discount = 1.0 - down_odds
            market_price = down_odds
        discounts.append(discount)

    avg_d = sum(discounts) / len(discounts)
    sorted_d = sorted(discounts)
    median_d = sorted_d[len(sorted_d) // 2]
    min_d = min(discounts)
    max_d = max(discounts)

    print(f"  市场折扣（距离真实胜率的差距）统计:")
    print(f"  均值   = {avg_d:.3f}")
    print(f"  中位数 = {median_d:.3f}")
    print(f"  最小值 = {min_d:.3f}")
    print(f"  最大值 = {max_d:.3f}")
    print()

    if avg_d > 0.05:
        print("✅ 市场存在显著折扣，末期套利策略有效！")
    elif avg_d > 0.02:
        print("🟡 市场折扣较小，策略利润空间有限，但仍可尝试。")
    else:
        print("❌ 市场折扣接近0，说明市场已高效定价，策略难以盈利。")


if __name__ == "__main__":
    asyncio.run(main())
