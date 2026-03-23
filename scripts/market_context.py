"""
历史行情背景模块 (MarketContext)

每个5分钟窗口开盘时调用 refresh()，提供：
  - 过去 30/90 分钟 BTC 趋势方向和强度（线性回归）
  - 近期波动率水平（ATR）
  - 信号方向与历史趋势的一致性评估和建议 gap 门槛

用途（见 doc/PHASE2_HISTORICAL_CONTEXT.md）：
  - 用途A：趋势一致性过滤器（顺势信号更可靠）
  - 用途B：波动率过滤器（高波动时 gap 更可靠）
  - 用途C：PtB 偏差估算参考（强趋势时偏差更大）

注意：此模块只叠加评分信息，不改变核心信号逻辑。
"""

import math
import os
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_BINANCE_KLINES_URL = (
    "https://api.binance.com/api/v3/klines"
    "?symbol=BTCUSDT&interval=1m&limit=95"
)
_TIMEOUT = 8


class MarketContext:
    """
    历史K线上下文管理器，每个5分钟窗口开盘时 refresh() 一次。
    线程不安全（单线程 asyncio 循环中使用）。
    """

    def __init__(self, session: requests.Session | None = None):
        self._session = session
        self._candles: list[dict] = []
        self._fetch_ts: int = 0

    # ─── 数据获取 ────────────────────────────────────────────────

    def _get_session(self) -> requests.Session:
        if self._session:
            return self._session
        s = requests.Session()
        retry = Retry(
            total=2, backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        s.mount("http://", HTTPAdapter(max_retries=retry))
        s.mount("https://", HTTPAdapter(max_retries=retry))
        proxy = os.environ.get("PROXY_URL", "").strip()
        if proxy:
            s.proxies = {"http": proxy, "https": proxy}
        return s

    def fetch_klines(self) -> list[dict]:
        """从 Binance 获取最近 95 根1分钟K线（覆盖90分钟 + 5根缓冲）。"""
        try:
            resp = self._get_session().get(_BINANCE_KLINES_URL, timeout=_TIMEOUT)
            resp.raise_for_status()
            raw = resp.json()
        except Exception as e:
            print(f"  [MarketContext] K线获取失败: {e}")
            return []
        return [
            {
                "ts":    k[0] // 1000,
                "open":  float(k[1]),
                "high":  float(k[2]),
                "low":   float(k[3]),
                "close": float(k[4]),
                "vol":   float(k[5]),
            }
            for k in raw
        ]

    def refresh(self, force: bool = False) -> bool:
        """
        刷新K线缓存。
          force=True  强制重新拉取（窗口开盘时使用）
          force=False 60秒内不重复拉取（轮询时使用）
        返回 True 表示有有效数据。
        """
        now = int(time.time())
        if not force and self._candles and (now - self._fetch_ts) < 60:
            return True
        candles = self.fetch_klines()
        if candles:
            self._candles = candles
            self._fetch_ts = now
            return True
        return bool(self._candles)  # 失败时保留旧数据

    # ─── 技术指标计算 ────────────────────────────────────────────

    def _tail(self, n: int) -> list[dict]:
        """取最后 n 根K线。"""
        return self._candles[-n:] if len(self._candles) >= n else self._candles[:]

    def calc_trend(self, window_minutes: int = 30) -> float:
        """
        趋势强度：+1.0（强上涨）到 -1.0（强下跌）。

        算法：
          1. 对过去 window_minutes 根K线收盘价做线性回归，取斜率（%/分钟）
          2. 计算总变化幅度（%）
          3. tanh(斜率 × 20 + 总变化 × 2) 映射到 [-1, +1]

        tanh 系数经验校准：
          斜率 ≈ +0.02%/min 或总变化 ≈ +0.15% → score ≈ +0.5（中等上涨）
          斜率 ≈ +0.05%/min 或总变化 ≈ +0.40% → score ≈ +0.9（强上涨）
        """
        candles = self._tail(window_minutes)
        if len(candles) < 5:
            return 0.0

        closes = [c["close"] for c in candles]
        n = len(closes)
        x_mean = (n - 1) / 2
        y_mean = sum(closes) / n

        num = sum((i - x_mean) * (closes[i] - y_mean) for i in range(n))
        den = sum((i - x_mean) ** 2 for i in range(n))
        slope_pct = (num / den / closes[0] * 100) if den > 0 else 0.0

        total_pct = (closes[-1] - closes[0]) / closes[0] * 100
        return round(math.tanh(slope_pct * 20 + total_pct * 2), 3)

    def calc_atr(self, window_minutes: int = 30) -> float:
        """
        平均真实波幅（ATR），以百分比表示。
        ATR per candle = (high - low) / close × 100%
        """
        candles = self._tail(window_minutes)
        if not candles:
            return 0.0
        trs = [(c["high"] - c["low"]) / c["close"] * 100 for c in candles]
        return round(sum(trs) / len(trs), 4)

    def get_context(self) -> dict | None:
        """
        返回完整历史背景快照。
        若无K线数据返回 None。
        """
        if not self._candles:
            return None

        trend_30m = self.calc_trend(30)
        trend_90m = self.calc_trend(90)
        atr_30m   = self.calc_atr(30)
        atr_90m   = self.calc_atr(90)
        vol_ratio = round(atr_30m / atr_90m, 3) if atr_90m > 0 else 1.0

        closes = [c["close"] for c in self._candles]
        price_5m_ago  = closes[-6]  if len(closes) >= 6  else closes[0]
        price_30m_ago = closes[-31] if len(closes) >= 31 else closes[0]

        # 波动率等级（基于 BTC 5m 历史统计）
        # high  > 0.10% ATR per candle → 活跃行情
        # low   < 0.05% ATR per candle → 横盘整理
        vol_level = (
            "high"   if atr_30m > 0.10 else
            "medium" if atr_30m > 0.05 else
            "low"
        )

        return {
            "trend_30m":     trend_30m,
            "trend_90m":     trend_90m,
            "atr_30m":       atr_30m,
            "atr_90m":       atr_90m,
            "vol_ratio":     vol_ratio,
            "vol_level":     vol_level,
            "price_5m_ago":  price_5m_ago,
            "price_30m_ago": price_30m_ago,
        }

    # ─── 信号可信度评估 ──────────────────────────────────────────

    def signal_confidence(
        self,
        signal_dir: str,  # "UP" 或 "DOWN"
        gap_pct: float,
        minute: int,
    ) -> dict:
        """
        综合评估当前套利信号的历史背景可信度。

        返回字段：
          score                      0.0–1.0，越高越可信
          trend_aligned              True（顺势）/ False（逆势）/ None（震荡）
          vol_level                  "high" / "medium" / "low"
          vol_ratio                  ATR-30m / ATR-90m（>1 波动扩张）
          trend_30m / trend_90m      趋势强度值
          recommended_gap_threshold  建议的最低 gap 门槛（%）
          note                       日志用简短说明

        评分逻辑：
          基础分 0.5
          + 趋势顺势 +0.15 / 逆势 -0.20 / 震荡 0
          + 高波动   +0.10 / 低波动 -0.10
          + 分钟     +0.04 × min(minute, 4)（最多 +0.16）
        """
        ctx = self.get_context()
        if not ctx:
            return {
                "score": 0.5,
                "trend_aligned": None,
                "trend_30m": 0.0,
                "trend_90m": 0.0,
                "atr_30m": 0.0,
                "vol_ratio": 1.0,
                "vol_level": "unknown",
                "price_5m_ago": None,
                "price_30m_ago": None,
                "recommended_gap_threshold": 0.05,
                "note": "无历史数据",
            }

        trend_30m = ctx["trend_30m"]
        vol_level = ctx["vol_level"]
        # 兼容 bot 传入的 "Up"/"Down"（Enum）与 "UP"/"DOWN"
        signal_up = signal_dir.upper() == "UP"

        # ── 趋势一致性 ──
        # 趋势值 > +0.15 视为上涨，< -0.15 视为下跌，其余视为震荡
        if trend_30m > 0.15:
            trend_aligned = signal_up           # 上涨趋势：UP顺势，DOWN逆势
        elif trend_30m < -0.15:
            trend_aligned = not signal_up       # 下跌趋势：DOWN顺势，UP逆势
        else:
            trend_aligned = None                # 震荡（中性）

        # ── 评分 ──
        trend_bonus  = 0.15 if trend_aligned is True else (-0.20 if trend_aligned is False else 0.0)
        vol_bonus    = {"high": 0.10, "medium": 0.0, "low": -0.10}.get(vol_level, 0.0)
        minute_bonus = 0.04 * min(minute, 4)
        score        = max(0.0, min(1.0, 0.5 + trend_bonus + vol_bonus + minute_bonus))

        # ── 建议 gap 门槛 ──
        if trend_aligned is False:
            rec_threshold = 0.15    # 逆势：提高门槛，减少误判
        elif vol_level == "low":
            rec_threshold = 0.10    # 低波动：中等门槛
        else:
            rec_threshold = 0.05    # 标准门槛

        # ── 说明文字 ──
        if trend_aligned is True:
            trend_note = "趋势" + ("↑" if signal_up else "↓") + "顺势"
        elif trend_aligned is False:
            trend_note = "⚠逆势" + ("↑" if not signal_up else "↓")
        else:
            trend_note = "趋势震荡"

        vol_note = {"high": "波动↑", "medium": "波动中", "low": "波动↓"}.get(vol_level, "")
        note = f"{trend_note} {vol_note}".strip()

        return {
            "score":                     round(score, 2),
            "trend_aligned":             trend_aligned,
            "trend_30m":                 trend_30m,
            "trend_90m":                 ctx["trend_90m"],
            "atr_30m":                   ctx["atr_30m"],
            "vol_ratio":                 ctx["vol_ratio"],
            "vol_level":                 vol_level,
            "price_5m_ago":              ctx["price_5m_ago"],
            "price_30m_ago":             ctx["price_30m_ago"],
            "recommended_gap_threshold": rec_threshold,
            "note":                      note,
        }
