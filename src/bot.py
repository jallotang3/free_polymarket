"""
主入口 — 事件循环与调度器

运行方式：
  python -m src.bot              # 默认 paper 模式
  python -m src.bot --mode paper  # 纸面交易
  python -m src.bot --mode live   # 实盘（需配置钱包）
  python -m src.bot --capital 500 # 指定初始资金（paper 专用）
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# 使 src 包可从项目根目录直接运行
_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from src.config import cfg
from src.data_feed import (
    BinancePriceFeed, PriceCache,
    current_window_ts, get_market_info, get_orderbook,
    seconds_into_window, seconds_remaining,
)
from src.strategy import Direction, LateStageArbitrageStrategy, RiskManager
from src.executor import TradeDB, TradeRecord, make_executor
from src.monitor import dashboard, notifier, setup_logging

logger = logging.getLogger("bot")


# ─────────────────────────────────────────────
# 主 Bot 类
# ─────────────────────────────────────────────

class PolymarketBot:
    def __init__(self, mode: str, initial_capital: float):
        self.mode = mode
        self.initial_capital = initial_capital

        # 数据层
        self.price_feed = BinancePriceFeed()
        self.price_cache = PriceCache(ttl_secs=2)

        # 策略 & 风控
        self.strategy = LateStageArbitrageStrategy(initial_capital)
        self.risk     = RiskManager(initial_capital)

        # 执行层
        Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db       = TradeDB(cfg.db_path)
        self.executor = make_executor(mode, self.db)

        # 状态
        self._open_trades: dict[int, TradeRecord] = {}  # window_ts → TradeRecord
        self._market_cache: dict[int, object] = {}      # window_ts → MarketInfo
        self._running = False
        self._last_window_ts: Optional[int] = None
        self._price_to_beat: Optional[float] = None

    # ── 生命周期 ──

    async def start(self):
        self._running = True
        logger.info("Bot 启动 | 模式=%s | 资金=%.2f", self.mode, self.initial_capital)
        notifier.notify(notifier.system_start(self.mode, self.initial_capital))

        # 验证配置
        issues = cfg.validate()
        if issues:
            for issue in issues:
                logger.error("配置错误: %s", issue)
            sys.exit(1)

        # 并发启动
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self.price_feed.run(),     name="price_feed")
            tg.create_task(notifier.run(),            name="telegram")
            tg.create_task(self._main_loop(),         name="main_loop")
            tg.create_task(self._settlement_loop(),   name="settlement")

    def stop(self, reason: str = "手动停止"):
        self._running = False
        self.price_feed.stop()
        stats = self.risk.stats
        total_pnl = stats["pnl"]
        logger.info("Bot 停止 | %s | 总 PnL=%.2f", reason, total_pnl)
        notifier.notify(notifier.system_stop(reason, total_pnl))

    # ── 主循环：每 5 秒轮询一次 ──

    async def _main_loop(self):
        logger.info("主循环启动，轮询间隔 %ds", cfg.poll_interval_secs)

        # 等待价格数据就绪
        price = await self.price_feed.wait_for_price(timeout=15)
        if price is None:
            logger.warning("WebSocket 价格超时，回退到 REST 模式")

        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error("主循环异常: %s", e, exc_info=True)
            await asyncio.sleep(cfg.poll_interval_secs)

    async def _tick(self):
        now = int(time.time())
        window_ts = current_window_ts()
        elapsed   = seconds_into_window()
        remaining = seconds_remaining()

        # ── 1. 获取 BTC 实时价格 ──
        btc_price = self.price_feed.price or await self.price_cache.get_async()
        if btc_price is None:
            logger.warning("无法获取 BTC 价格，跳过本次 tick")
            return

        # ── 2. 新窗口开盘处理 ──
        if window_ts != self._last_window_ts:
            await self._on_new_window(window_ts, btc_price)
            self._last_window_ts = window_ts

        # ── 3. 同步策略资金 ──
        self.strategy.update_capital(self.risk.current_capital)

        # ── 4. 仅在入场窗口内评估信号 ──
        if not (cfg.entry_window_start <= elapsed <= cfg.entry_window_end):
            gap_pct = None
            if self._price_to_beat and btc_price:
                gap_pct = (btc_price - self._price_to_beat) / self._price_to_beat * 100
            # 每分钟输出一次心跳日志，证明 Bot 正在运行
            if elapsed % 60 < cfg.poll_interval_secs:
                gap_str = f"{gap_pct:+.3f}%" if gap_pct is not None else "N/A"
                logger.debug(
                    "⏳ 等待入场窗口 [%ds/%ds]  BTC=$%.2f  gap=%s  资金=$%.2f",
                    elapsed, 300, btc_price, gap_str, self.risk.current_capital,
                )
            dashboard.maybe_print(
                btc_price, gap_pct, window_ts,
                self.risk.current_capital, self.risk.stats, self.mode
            )
            return

        # ── 5. 风控检查 ──
        allowed, reason = self.risk.allow_trade()
        if not allowed:
            logger.warning("风控拦截: %s", reason)
            return

        # ── 6. 获取市场数据（每分钟刷新一次）──
        market = await self._get_market(window_ts, elapsed)
        if market is None:
            logger.warning("窗口 %d 市场数据获取失败，跳过本次 tick（elapsed=%ds）", window_ts, elapsed)
            return

        # ── 7. 获取订单簿（仅在信号可能触发时）──
        orderbook = None
        if self._price_to_beat:
            gap_abs = abs((btc_price - self._price_to_beat) / self._price_to_beat * 100)
            if gap_abs >= cfg.min_gap_pct:
                direction_token = market.up_token if btc_price > self._price_to_beat else market.down_token
                orderbook = await asyncio.get_event_loop().run_in_executor(
                    None, get_orderbook, direction_token
                )

        # ── 8. 评估信号 ──
        gap_pct = (btc_price - self._price_to_beat) / self._price_to_beat * 100 if self._price_to_beat else 0
        logger.debug(
            "🔍 评估信号 窗口%d | BTC=$%.2f gap=%+.3f%% Up=%.3f Down=%.3f elapsed=%ds",
            window_ts, btc_price, gap_pct, market.up_odds, market.down_odds, elapsed,
        )
        signal = self.strategy.evaluate(
            window_ts=window_ts,
            btc_price=btc_price,
            market=market,
            orderbook=orderbook,
            seconds_elapsed=elapsed,
        )

        if signal is None or not signal.is_valid:
            return

        # ── 9. 执行下单 ──
        trade = await self.executor.place(signal, window_ts)
        if trade is None:
            logger.error("下单失败")
            return

        self._open_trades[window_ts] = trade
        self.strategy.mark_traded(window_ts, signal.direction)

        notifier.notify(notifier.trade_opened(
            self.mode, signal.direction.value, signal.bet_amount,
            signal.market_price, signal.ev_per_unit, signal.gap_pct
        ))

    # ── 结算循环：窗口结束后查询结果 ──

    async def _settlement_loop(self):
        """每 30 秒检查一次是否有已关闭的窗口需要结算"""
        _heartbeat_count = 0
        while self._running:
            await asyncio.sleep(30)
            _heartbeat_count += 1
            pending = len(self._open_trades)
            if pending > 0 or _heartbeat_count % 10 == 0:  # 有持仓时每次打，无持仓每 5 分钟一次
                logger.debug(
                    "结算检查 | 待结算=%d笔  累计交易=%d笔  PnL=%+.2f",
                    pending, self.risk.stats["trades"], self.risk.stats["pnl"],
                )
            await self._settle_closed_windows()

    async def _settle_closed_windows(self):
        now = int(time.time())
        to_settle = [
            wts for wts in list(self._open_trades.keys())
            if now > wts + 310  # 窗口结束后 10 秒才查结果（等 Chainlink 更新）
        ]

        for wts in to_settle:
            trade = self._open_trades.pop(wts)
            result = await self._fetch_result(wts, trade)
            if result is None:
                logger.warning("窗口 %d 结果查询失败，稍后重试", wts)
                self._open_trades[wts] = trade  # 放回重试
                continue

            pnl = await self.executor.settle(trade, result)
            self.risk.record_result(result == trade.direction, pnl)

            notifier.notify(notifier.trade_settled(
                self.mode, trade.direction, result,
                pnl, self.risk.win_rate, self.risk.stats["pnl"]
            ))

    async def _fetch_result(self, window_ts: int, trade: TradeRecord) -> Optional[str]:
        """查询窗口的实际结算结果"""
        loop = asyncio.get_event_loop()
        market = await loop.run_in_executor(None, get_market_info, window_ts)
        if market is None:
            return None
        if not market.closed:
            logger.debug("窗口 %d 尚未结算", window_ts)
            return None
        # 赔率为 1 的方向就是赢家
        if market.up_odds >= 0.99:
            return "Up"
        elif market.down_odds >= 0.99:
            return "Down"
        # 中间状态：根据赔率判断（>0.5 一侧视为赢家）
        return "Up" if market.up_odds > market.down_odds else "Down"

    # ── 辅助方法 ──

    async def _on_new_window(self, window_ts: int, btc_price: float):
        """新窗口开盘时的初始化逻辑"""
        self._price_to_beat = btc_price
        self._market_cache.pop(window_ts - 300, None)  # 清理上个窗口缓存

        self.strategy.set_price_to_beat(window_ts, btc_price)
        logger.info(
            "── 新窗口 ts=%d  PtB=$%.2f  资金=%.2f  已交易=%d笔 ──",
            window_ts, btc_price, self.risk.current_capital, self.risk.stats["trades"]
        )

    async def _get_market(self, window_ts: int, elapsed: int) -> Optional[object]:
        """获取市场数据，按分钟缓存（减少 API 请求）"""
        cache_key = f"{window_ts}_{elapsed // 60}"
        if cache_key in self._market_cache:
            return self._market_cache[cache_key]

        loop = asyncio.get_event_loop()
        market = await loop.run_in_executor(None, get_market_info, window_ts)
        if market:
            self._market_cache[cache_key] = market
        return market


# ─────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Polymarket BTC 末期套利机器人")
    parser.add_argument(
        "--mode", choices=["paper", "live"], default=cfg.mode,
        help="运行模式：paper=纸面交易（默认），live=实盘"
    )
    parser.add_argument(
        "--capital", type=float, default=1000.0,
        help="初始模拟资金（paper 模式专用，默认 $1000）"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="开启 DEBUG 日志"
    )
    return parser.parse_args()


async def _run(mode: str, capital: float):
    bot = PolymarketBot(mode=mode, initial_capital=capital)

    # 优雅退出
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: bot.stop("收到退出信号"))

    try:
        await bot.start()
    except* KeyboardInterrupt:
        bot.stop("KeyboardInterrupt")
    except* Exception as eg:
        for e in eg.exceptions:
            logger.error("致命错误: %s", e, exc_info=True)
        bot.stop(f"致命错误: {eg.exceptions[0]}")


def main():
    args = parse_args()
    setup_logging(logging.DEBUG if args.debug else logging.INFO)

    mode = args.mode
    capital = args.capital

    logger.info("=" * 55)
    logger.info("  Polymarket BTC 末期套利 Bot")
    logger.info("  模式: %s  资金: $%.2f", mode.upper(), capital)
    logger.info("  策略: gap >= %.2f%%  边际 >= %.2f",
                cfg.min_gap_pct, cfg.entry_margin)
    logger.info("=" * 55)

    if mode == "live":
        if not cfg.has_wallet:
            logger.error("实盘模式需要在 .env 中配置 PRIVATE_KEY 和 WALLET_ADDRESS")
            sys.exit(1)
        logger.warning("⚠️  实盘模式：将使用真实资金！")
        try:
            confirm = input("输入 YES 确认启动实盘: ").strip()
            if confirm != "YES":
                logger.info("已取消")
                sys.exit(0)
        except (EOFError, KeyboardInterrupt):
            sys.exit(0)

    asyncio.run(_run(mode, capital))


if __name__ == "__main__":
    main()
