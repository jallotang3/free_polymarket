"""
Phase 1 数据采集脚本 — 无需钱包，只读，免费

数据源优先级（按可靠性从高到低）：
  1. Chainlink 链上标准预言机 (Polygon PoS)  — 同属 Chainlink DON 网络，最接近真实 PtB
  2. CryptoCompare 聚合均价                  — 多交易所加权，偏差通常 < $10
  3. Binance 现货价格                         — 最快，但单交易所，系统性低于 Chainlink ~$20-50

Polymarket 使用 Chainlink Data Streams（付费，亚秒级），但链上标准预言机同属同一 DON 网络，
是免费可访问数据中最接近真实结算价的来源。

代理支持（可选）：
  在 .env 中设置 PROXY_URL，格式：
    http://127.0.0.1:7890        # HTTP 代理
    socks5://127.0.0.1:1080      # SOCKS5 代理（需安装 PySocks: pip install PySocks）
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

try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(_env_path)
except ImportError:
    pass

# ────────────────────────────────────────────────────────────
DB_PATH      = "observations.db"
HEADERS      = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
POLL_INTERVAL = 5   # 秒
TIMEOUT       = 8   # HTTP 请求超时秒数
CL_FRESH_SECS = 45  # Chainlink 链上价格在此秒数内视为"有效"
# ────────────────────────────────────────────────────────────

# Chainlink BTC/USD 链上聚合器（Polygon PoS）
# 合约: https://data.chain.link/feeds/polygon/mainnet/btc-usd
# 更新规则：价格偏差 ≥ 0.5% 或 心跳 3600s，BTC 波动时实际约 15-60s 更新一次
_CL_CONTRACT = "0xc907E116054Ad103354f2D350FD2514433D57F6f"
_CL_SELECTOR = "0xfeaf968c"  # latestRoundData()
_CL_RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://1rpc.io/matic",
    "https://rpc.ankr.com/polygon",
]
_cl_rpc_idx = 0


def _build_session() -> requests.Session:
    """构建全局 HTTP Session，支持代理和自动重试（GET + POST）。"""
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    proxy_url = os.environ.get("PROXY_URL", "").strip()
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}
        print(f"  [代理] 已启用: {proxy_url}")
    else:
        sys_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        print(f"  [代理] 检测到系统代理: {sys_proxy}" if sys_proxy
              else "  [代理] 直连模式（如需代理，在 .env 中设置 PROXY_URL）")
    return session


_SESSION: requests.Session | None = None


def get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = _build_session()
    return _SESSION


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS observations (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                 INTEGER NOT NULL,
            window_ts          INTEGER NOT NULL,
            minute_in_window   INTEGER NOT NULL,
            btc_price          REAL,
            cc_price           REAL,
            cl_onchain_price   REAL,
            cl_onchain_age     INTEGER,
            price_to_beat      REAL,
            gap_pct            REAL,
            bn_gap_pct         REAL,
            cl_onchain_gap_pct REAL,
            up_odds            REAL,
            down_odds          REAL,
            volume             REAL,
            condition_id       TEXT
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
    # 向后兼容：为旧表补充新列（已存在则忽略）
    migrations = [
        ("chainlink_price",    "REAL"),
        ("cl_price_to_beat",   "REAL"),
        ("cl_gap_pct",         "REAL"),
        ("price_divergence",   "REAL"),
        ("cc_price",           "REAL"),
        ("cl_onchain_price",   "REAL"),
        ("cl_onchain_age",     "INTEGER"),
        ("bn_gap_pct",         "REAL"),
        ("cl_onchain_gap_pct", "REAL"),
    ]
    existing = {row[1] for row in conn.execute("PRAGMA table_info(observations)")}
    for col, typ in migrations:
        if col not in existing:
            conn.execute(f"ALTER TABLE observations ADD COLUMN {col} {typ}")
            print(f"  [DB 迁移] 添加列: {col}")
    conn.commit()
    return conn


def fetch_json(url: str) -> dict | list | None:
    try:
        resp = get_session().get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.ProxyError as e:
        print(f"  [代理错误] {url[:60]}... → {e}")
    except requests.exceptions.SSLError as e:
        print(f"  [SSL错误] {url[:60]}... → {e}")
    except requests.exceptions.Timeout:
        print(f"  [超时] {url[:60]}...")
    except requests.exceptions.ConnectionError as e:
        print(f"  [连接错误] {url[:60]}... → {e}")
    except Exception as e:
        print(f"  [HTTP error] {url[:60]}... → {e}")
    return None


def get_btc_price() -> float | None:
    """Binance BTC/USDT 实时价格（最快，单交易所）"""
    data = fetch_json("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
    if data:
        return float(data["price"])
    return None


def get_cryptocompare_price() -> float | None:
    """CryptoCompare BTC/USD 多交易所加权均价"""
    data = fetch_json("https://min-api.cryptocompare.com/data/price?fsym=BTC&tsyms=USD")
    if data and "USD" in data:
        return float(data["USD"])
    return None


def get_chainlink_onchain_price() -> tuple[float | None, int | None]:
    """
    从 Polygon 链上读取 Chainlink BTC/USD 聚合器价格。
    返回 (price, oracle_updated_at_timestamp)，失败返回 (None, None)。

    注意：此价格来自链上标准聚合器（每60s心跳或0.5%偏差触发更新），
    与 Polymarket 使用的 Chainlink Data Streams（亚秒级）略有差异，
    但同属 Chainlink DON 网络，是免费数据中最接近真实 PtB 的来源。
    实测 BTC 波动时约每 15-60 秒更新一次。
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
            resp = get_session().post(rpc, json=payload, timeout=TIMEOUT)
            result = resp.json().get("result", "")
            if result and len(result) >= 130:
                data = result[2:]
                # latestRoundData() 返回顺序（每项32字节/64hex）:
                # [0]roundId  [1]answer  [2]startedAt  [3]updatedAt  [4]answeredInRound
                price      = int(data[64:128], 16) / 1e8
                updated_at = int(data[128:192], 16)
                return price, updated_at
        except Exception:
            pass
        _cl_rpc_idx += 1
    return None, None


def get_polymarket_window(window_ts: int) -> dict | None:
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
            "up_token":  tokens[0],
            "down_token": tokens[1],
            "up_odds":   float(prices[0]),
            "down_odds":  float(prices[1]),
            "volume":    float(m.get("volume", 0)),
            "active":    m.get("active", False),
            "closed":    m.get("closed", False),
        }
    except (KeyError, IndexError, json.JSONDecodeError):
        return None


def get_current_window_ts() -> int:
    now = int(time.time())
    return now - (now % 300)


def log_observation(conn: sqlite3.Connection, obs: dict):
    conn.execute("""
        INSERT INTO observations
        (ts, window_ts, minute_in_window,
         btc_price, cc_price, cl_onchain_price, cl_onchain_age,
         price_to_beat,
         gap_pct, bn_gap_pct, cl_onchain_gap_pct,
         up_odds, down_odds, volume, condition_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        obs["ts"], obs["window_ts"], obs["minute_in_window"],
        obs["btc_price"], obs.get("cc_price"), obs.get("cl_onchain_price"), obs.get("cl_onchain_age"),
        obs["price_to_beat"],
        obs["gap_pct"], obs.get("bn_gap_pct"), obs.get("cl_onchain_gap_pct"),
        obs["up_odds"], obs["down_odds"], obs["volume"], obs["condition_id"],
    ))
    conn.commit()


def analyze_opportunity(
    obs: dict,
    btc_trend_pct: float | None = None,
    bn_gap_delta: float | None = None,
    ptb_delay_secs: int = 0,
    cl_gap: float | None = None,
    cl_age: int | None = None,
) -> str:
    """
    实时判断当前是否有套利机会。

    gap 来源优先级（由调用方传入）：
      cl_gap (Chainlink 链上, cl_age < CL_FRESH_SECS) > obs["gap_pct"] (CryptoCompare) > Binance
    """
    # 选择最可靠的 gap 来源
    cl_fresh = cl_age is not None and cl_age < CL_FRESH_SECS
    if cl_fresh and cl_gap is not None:
        gap = cl_gap
        gap_src = "CL链上"
    else:
        gap = obs["gap_pct"]  # CryptoCompare gap
        gap_src = "CC"

    minute    = obs["minute_in_window"]
    up_odds   = obs["up_odds"]
    down_odds = obs["down_odds"]
    bn_gap    = obs.get("bn_gap_pct", gap)  # Binance gap，用于趋势冲突检查

    # ── PtB 延迟过大：基准价失真 ──
    if ptb_delay_secs > 60:
        return (f"⏳ PtB延迟{ptb_delay_secs}s，基准失真，忽略gap信号。"
                f"仅赔率跳变信号可信。")

    # ── 市场方向软冲突检查 ──
    # 区分「赔率已跳变（Chainlink确认）」和「静止偏置」：
    #   - 静止偏置阈值更高(0.65)，避免无根据的赔率偏置压制正确gap信号
    #   - 赔率跳变后阈值降低(0.58)，尊重 Chainlink 的方向确认
    if abs(gap) >= 0.05 and minute >= 2:
        signal_up     = gap > 0
        opposing_odds = up_odds if not signal_up else down_odds
        soft_threshold = 0.58 if obs.get("_odds_jumped", False) else 0.65
        if opposing_odds > soft_threshold:
            signal_dir = "UP" if signal_up else "DOWN"
            opp_dir    = "DOWN" if signal_up else "UP"
            jump_note  = "(Chainlink赔率跳变确认)" if obs.get("_odds_jumped") else f"(静止偏置/{gap_src}参考)"
            return (f"⚠️ 市场软冲突: {gap_src}gap→{signal_dir} 但赔率{opp_dir}={opposing_odds:.2f}"
                    f">{soft_threshold}{jump_note}，不操作")

    # ── CL vs CC 方向不一致提示（不阻断，仅记录，CL 已优先使用）──
    cc_gap = obs.get("gap_pct", 0)
    cl_cc_conflict = (
        cl_fresh and cl_gap is not None
        and abs(cl_gap) >= 0.03 and abs(cc_gap) >= 0.03
        and (cl_gap > 0) != (cc_gap > 0)
    )

    # ── 市场方向强冲突：gap 方向与强势赔率（>0.60）相反 ──
    if abs(gap) >= 0.05 and minute >= 3:
        gap_up           = gap > 0
        market_up_dom    = up_odds > 0.60
        market_dn_dom    = down_odds > 0.60
        if (gap_up and market_dn_dom) or (not gap_up and market_up_dom):
            market_dir   = "UP" if market_up_dom else "DOWN"
            market_price = up_odds if market_up_dom else down_odds

            # 趋势冲突：BTC 实时价格趋势与赔率方向相反
            trend_opposes = False
            trend_note    = ""
            if btc_trend_pct is not None and abs(btc_trend_pct) > 0.01:
                btc_rising = btc_trend_pct > 0
                if (market_dir == "DOWN" and btc_rising) or (market_dir == "UP" and not btc_rising):
                    trend_opposes = True
                    trend_note = f"BTC趋势{btc_trend_pct:+.3f}%/poll反向"

            # Binance gap 与赔率反向且幅度 > 0.08%
            # 当 CL 和 BN 都显示反向时，gap 可能已回穿真实 PtB
            BN_COUNTER_THRESHOLD = 0.08
            bn_counter = False
            if (market_dir == "DOWN" and bn_gap > BN_COUNTER_THRESHOLD) or \
               (market_dir == "UP"   and bn_gap < -BN_COUNTER_THRESHOLD):
                bn_counter = True
                trend_note = (trend_note + f" BN-gap反向{bn_gap:+.3f}%>{BN_COUNTER_THRESHOLD:.2f}%").strip()

            if trend_opposes or bn_counter:
                return (f"⚠️ 赔率存疑! 方向={market_dir}(赔率={market_price:.2f}) "
                        f"但{trend_note}，gap可能已回穿Chainlink PtB，勿操作")

            # 赔率强度检查：0.60-0.72 属中等确信，历史上可在分4彻底逆转
            STRONG_ODDS_THRESHOLD = 0.72
            if market_price < STRONG_ODDS_THRESHOLD:
                return (f"⚪ 赔率不够强: 方向={market_dir} 赔率={market_price:.2f}"
                        f"<{STRONG_ODDS_THRESHOLD}，中等确信度，不操作")

            # 强冲突但赔率够强：以赔率为准
            theo_wr = 0.897 if market_price < 0.85 else 0.968
            ev = theo_wr * (1 - market_price) - (1 - theo_wr) * market_price
            if ev > 0.05:
                return (f"🟢 跟赔率! 方向={market_dir}(赔率主导) 理论≈{theo_wr:.1%} "
                        f"赔率={market_price:.2f} EV={ev:+.3f}  [Chainlink赔率强确认]")
            return f"⚪ 跟赔率EV不足 方向={market_dir} 赔率={market_price:.2f} EV={ev:+.3f}"

    # ── 理论胜率查表（末期套利核心逻辑）──
    def theo_win_rate(gap_abs: float, min_w: int) -> float:
        if min_w < 3:
            return 0.5
        if gap_abs >= 0.30: return 0.995
        if gap_abs >= 0.20: return 0.982
        if gap_abs >= 0.15: return 0.979
        if gap_abs >= 0.10: return 0.968
        if gap_abs >= 0.05: return 0.897
        return 0.5

    gap_abs      = abs(gap)
    theo_wr      = theo_win_rate(gap_abs, minute)
    direction    = "UP" if gap > 0 else "DOWN"
    market_price = up_odds if gap > 0 else down_odds

    if theo_wr < 0.85:
        return "⚪ 无信号"

    ev       = theo_wr * (1 - market_price) - (1 - theo_wr) * market_price
    fee_frac = 0.25 * (market_price * (1 - market_price)) ** 2
    ev_fee   = ev - theo_wr * fee_frac

    # 标注 gap 来源（非 CC 时显示）
    src_tag      = f"[{gap_src}]" if gap_src != "CC" else ""
    conflict_tag = " ⚠️CC方向不同" if cl_cc_conflict else ""

    if ev > 0.05:
        return (f"🟢 强套利! 方向={direction} 理论={theo_wr:.1%} 赔率={market_price:.2f} "
                f"EV={ev:+.3f}(含费≈{ev_fee:+.3f}){src_tag}{conflict_tag}")
    if ev > 0.02:
        return (f"🟡 弱套利  方向={direction} 理论={theo_wr:.1%} 赔率={market_price:.2f} "
                f"EV={ev:+.3f}(含费≈{ev_fee:+.3f}){src_tag}{conflict_tag}")
    if ev > 0:
        return (f"⚪ 微弱    方向={direction} 理论={theo_wr:.1%} 赔率={market_price:.2f} "
                f"EV={ev:+.3f}{src_tag}")
    return (f"🔴 无利润  方向={direction} 理论={theo_wr:.1%} 赔率={market_price:.2f} "
            f"EV={ev:+.3f} (市场已定价)")


async def main():
    print("=" * 65)
    print("Polymarket BTC 5m 数据采集器 — 含 Chainlink 链上价格")
    print("数据源: Chainlink链上(主) > CryptoCompare(次) > Binance(辅)")
    print("按 Ctrl+C 停止")
    print("=" * 65)

    conn = init_db()

    window_cache:    dict[int, dict]   = {}
    bn_ptb_cache:    dict[int, float]  = {}   # Binance 开盘价
    cc_ptb_cache:    dict[int, float]  = {}   # CryptoCompare 开盘价（兼容旧逻辑）
    cl_ptb_cache:    dict[int, float]  = {}   # Chainlink 链上开盘价（主用 PtB）
    ptb_delay_cache: dict[int, int]    = {}   # 开盘 PtB 记录时的延迟秒数

    prev_up_odds:      float | None = None
    prev_window_ts:    int | None   = None
    prev_btc_price:    float | None = None
    prev_bn_gap:       float | None = None
    window_odds_jumped: bool        = False

    # Chainlink 链上价格缓存（避免每次 poll 都触发失败重试）
    cl_last_price: float | None = None
    cl_last_oracle_ts: int | None = None  # oracle 更新时间戳

    try:
        while True:
            now        = int(time.time())
            window_ts  = get_current_window_ts()
            elapsed    = now - window_ts     # 当前窗口已过去的秒数
            minute     = elapsed // 60
            ts_str     = datetime.now(timezone.utc).strftime("%H:%M:%S")

            # ── 获取三路价格 ──
            btc_price = get_btc_price()
            cc_price  = get_cryptocompare_price()

            # Chainlink 链上：每次都尝试拉取（RPC 调用轻量级）
            cl_price, cl_oracle_ts = get_chainlink_onchain_price()
            if cl_price is not None:
                cl_last_price     = cl_price
                cl_last_oracle_ts = cl_oracle_ts
            else:
                # 失败时沿用上一次有效值
                cl_price     = cl_last_price
                cl_oracle_ts = cl_last_oracle_ts

            # Chainlink 数据的"年龄"（oracle 上次更新距现在多少秒）
            cl_age = int(now - cl_oracle_ts) if cl_oracle_ts else None
            cl_fresh = cl_age is not None and cl_age < CL_FRESH_SECS

            if btc_price is None:
                print(f"[{ts_str}] ⚠️  无法获取 BTC 价格，跳过")
                await asyncio.sleep(POLL_INTERVAL)
                continue

            # ── 新窗口切换：重置跨窗口状态 ──
            if window_ts != prev_window_ts:
                prev_up_odds       = None
                prev_btc_price     = None
                prev_bn_gap        = None
                window_odds_jumped = False
                prev_window_ts     = window_ts

            # ── 记录开盘 PtB（优先 Chainlink链上 > CC > Binance）──
            if window_ts not in bn_ptb_cache:
                bn_ptb_cache[window_ts] = btc_price
                cc_ptb_cache[window_ts] = cc_price or btc_price
                if cl_price and cl_fresh:
                    cl_ptb_cache[window_ts] = cl_price
                ptb_delay_cache[window_ts] = elapsed

                if elapsed <= POLL_INTERVAL:
                    quality = "精确"
                elif elapsed <= 30:
                    quality = f"轻微延迟({elapsed}s)"
                else:
                    quality = f"⚠️ 延迟较大({elapsed}s)"

                if cl_price and cl_fresh:
                    ptb_src = f"CL链上PtB=${cl_price:,.2f}"
                    ptb_note = f"(链上{cl_age}s前更新)"
                elif cc_price:
                    ptb_src = f"CC估算PtB=${cc_price or btc_price:,.2f}"
                    ptb_note = "⚠️ 真实Chainlink PtB以Polymarket页面为准"
                else:
                    ptb_src = f"Binance估算PtB=${btc_price:,.2f}"
                    ptb_note = "⚠️ 无法获取CC/CL，精度较低"

                print(f"[{ts_str}] 📌 新窗口开盘  {ptb_src}  [{quality}]  {ptb_note}")

            # ── 三源 gap 计算 ──
            bn_ptb = bn_ptb_cache.get(window_ts, btc_price)
            cc_ptb = cc_ptb_cache.get(window_ts, cc_price or btc_price)
            cl_ptb = cl_ptb_cache.get(window_ts)

            bn_gap_pct = (btc_price - bn_ptb) / bn_ptb * 100

            if cc_price and cc_ptb:
                cc_gap_pct = (cc_price - cc_ptb) / cc_ptb * 100
            else:
                cc_gap_pct = bn_gap_pct

            cl_onchain_gap_pct = None
            if cl_price and cl_ptb and cl_fresh:
                cl_onchain_gap_pct = (cl_price - cl_ptb) / cl_ptb * 100
            elif cl_price and cc_ptb and cl_fresh:
                # PtB 开盘时 CL 不可用，用 CC PtB 作为基准近似
                cl_onchain_gap_pct = (cl_price - cc_ptb) / cc_ptb * 100

            # 主用 gap（用于信号分析和数据库）
            if cl_onchain_gap_pct is not None and cl_fresh:
                primary_gap = cl_onchain_gap_pct
            elif cc_price:
                primary_gap = cc_gap_pct
            else:
                primary_gap = bn_gap_pct

            # ── 获取 Polymarket 赔率（每分钟缓存）──
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
                "ts":                now,
                "window_ts":         window_ts,
                "minute_in_window":  minute,
                "btc_price":         btc_price,
                "cc_price":          cc_price,
                "cl_onchain_price":  cl_price,
                "cl_onchain_age":    cl_age,
                "price_to_beat":     cl_ptb or cc_ptb or bn_ptb,
                "gap_pct":           primary_gap,
                "bn_gap_pct":        bn_gap_pct,
                "cl_onchain_gap_pct": cl_onchain_gap_pct,
                "up_odds":           mkt["up_odds"],
                "down_odds":         mkt["down_odds"],
                "volume":            mkt["volume"],
                "condition_id":      mkt["condition_id"],
            }

            log_observation(conn, obs)

            # ── 价格趋势计算 ──
            btc_trend_pct: float | None = None
            bn_gap_delta: float | None = None
            if prev_btc_price is not None:
                btc_trend_pct = (btc_price - prev_btc_price) / prev_btc_price * 100
            if prev_bn_gap is not None:
                bn_gap_delta = bn_gap_pct - prev_bn_gap
            prev_btc_price = btc_price
            prev_bn_gap    = bn_gap_pct

            # ── 赔率跳变检测（最可靠信号，不依赖 PtB 估算）──
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
                    window_odds_jumped = True
            prev_up_odds = mkt["up_odds"]

            # ── 赔率静止警告 ──
            window_odds_list = [
                v["up_odds"] for k, v in window_cache.items()
                if k.startswith(str(window_ts))
            ]
            odds_static_warn = ""
            if len(window_odds_list) >= 4:
                spread = max(window_odds_list) - min(window_odds_list)
                if spread < 0.02:
                    odds_static_warn = " 📊[赔率静止，流动性极低]"

            obs["_odds_jumped"] = window_odds_jumped

            ptb_delay = ptb_delay_cache.get(window_ts, 0)

            signal = analyze_opportunity(
                obs,
                btc_trend_pct=btc_trend_pct,
                bn_gap_delta=bn_gap_delta,
                ptb_delay_secs=ptb_delay,
                cl_gap=cl_onchain_gap_pct,
                cl_age=cl_age,
            )

            # ── 日志格式：优先展示 Chainlink 链上 gap ──
            gap_arrow = "↑" if primary_gap > 0 else ("↓" if primary_gap < 0 else "─")

            if cl_onchain_gap_pct is not None and cl_fresh:
                # CL 链上有效：显示 CL gap 和 BN gap
                cl_tag = f"CL/{cl_age}s"
                if abs(cl_onchain_gap_pct - bn_gap_pct) > 0.02:
                    gap_str = (f"{gap_arrow}{cl_onchain_gap_pct:+.3f}%({cl_tag}) "
                               f"/ {bn_gap_pct:+.3f}%(BN)")
                else:
                    gap_str = f"{gap_arrow}{cl_onchain_gap_pct:+.3f}%({cl_tag})"
            elif cc_price and abs(cc_gap_pct - bn_gap_pct) > 0.02:
                # CL 不可用时，退回 CC vs BN
                gap_str = f"{gap_arrow}{cc_gap_pct:+.3f}%(CC) / {bn_gap_pct:+.3f}%(BN)"
            else:
                gap_str = f"{gap_arrow}{bn_gap_pct:+.3f}%(BN)"

            cl_status = f"CL=${cl_price:,.0f}" if cl_price else "CL=N/A"

            print(
                f"[{ts_str}] 分{minute} | "
                f"BTC=${btc_price:,.1f} {cl_status} {gap_str} | "
                f"Up={mkt['up_odds']:.2f} Down={mkt['down_odds']:.2f} | "
                f"{signal}{odds_jump_signal}{odds_static_warn}"
            )

            await asyncio.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\n\n采集停止。生成统计报告...")
        _print_summary(conn)
    finally:
        conn.close()


def _print_summary(conn: sqlite3.Connection):
    rows = conn.execute("""
        SELECT minute_in_window, gap_pct, cl_onchain_gap_pct, up_odds, down_odds
        FROM observations
        WHERE ABS(gap_pct) >= 0.10 AND minute_in_window >= 3
    """).fetchall()

    if not rows:
        print("数据不足，请采集更多数据后再分析。")
        return

    print(f"\n总计 {len(rows)} 条 gap≥0.10% 的末期观察：")
    cl_available = sum(1 for r in rows if r[2] is not None)
    print(f"  其中 Chainlink链上gap 可用：{cl_available}/{len(rows)} 条")

    discounts = []
    for row in rows:
        minute, gap, cl_gap, up_odds, down_odds = row
        eff_gap  = cl_gap if cl_gap is not None else gap
        if eff_gap > 0:
            discounts.append(1.0 - up_odds)
        else:
            discounts.append(1.0 - down_odds)

    avg_d    = sum(discounts) / len(discounts)
    sorted_d = sorted(discounts)
    median_d = sorted_d[len(sorted_d) // 2]
    print(f"  市场折扣统计: 均值={avg_d:.3f}  中位数={median_d:.3f}  "
          f"最小={min(discounts):.3f}  最大={max(discounts):.3f}")

    if avg_d > 0.05:
        print("✅ 市场存在显著折扣，末期套利策略有效！")
    elif avg_d > 0.02:
        print("🟡 市场折扣较小，利润空间有限，但仍可尝试。")
    else:
        print("❌ 市场折扣接近0，策略难以盈利。")


if __name__ == "__main__":
    asyncio.run(main())
