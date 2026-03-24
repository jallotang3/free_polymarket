"""
主入口 — 事件循环与调度器

运行方式：
  python -m src.bot              # 默认 paper 模式
  python -m src.bot --mode paper  # 纸面交易
  python -m src.bot --mode live   # 实盘（需配置钱包）
  python -m src.bot --capital 500 # 指定初始资金（paper 专用）

信号路径：
  路径1（跟赔率）  ：gap ≥ 0.05% + minute ≥ 3 + gap 与赔率反向 → 赔率主导
  路径2（赔率强信号）：gap < 0.05% + 赔率单边 ≥ 0.72 + 分层时机 + 历史可信度过滤
  路径3（末期套利）  ：gap ≥ 0.10% + minute ≥ 3 → 理论胜率查表
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

_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from src.config import cfg
from src.data_feed import (
    BinancePriceFeed, PriceCache,
    current_window_ts, seconds_into_window, seconds_remaining,
    get_market_info, get_clob_midpoints, get_orderbook,
    get_chainlink_onchain_price, get_btc_price_rest, get_cryptocompare_price,
)
from src.strategy import Direction, LateStageArbitrageStrategy, RiskManager
from src.executor import TradeDB, TradeRecord, make_executor
from src.monitor import dashboard, notifier, setup_logging
from src.redeemer import AutoRedeemer

# 历史背景模块（可选）
try:
    from scripts.market_context import MarketContext
    _HAS_MC = True
except ImportError:
    try:
        sys.path.insert(0, str(_root / "scripts"))
        from market_context import MarketContext
        _HAS_MC = True
    except ImportError:
        _HAS_MC = False

logger = logging.getLogger("bot")

CL_FRESH_SECS = 45   # Chainlink 链上价格新鲜阈值（秒）


class PolymarketBot:
    def __init__(self, mode: str, initial_capital: float):
        self.mode            = mode
        self.initial_capital = initial_capital

        # 数据层
        self.price_feed  = BinancePriceFeed()
        self.price_cache = PriceCache(ttl_secs=2)

        # 策略 & 风控
        self.strategy = LateStageArbitrageStrategy(initial_capital)
        self.risk     = RiskManager(initial_capital)

        # 执行层
        Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db       = TradeDB(cfg.db_path)
        self.executor = make_executor(mode, self.db)

        # 历史背景模块（Phase 2）
        self.mc: Optional[MarketContext] = MarketContext() if _HAS_MC else None

        # 自动兑换模块（注入 Telegram 通知和余额查询回调）
        self.redeemer = AutoRedeemer(
            private_key          = cfg.private_key,
            wallet_address       = cfg.wallet_address,
            signature_type       = 0,   # EOA
            notify_callback      = notifier.notify if cfg.has_telegram else None,
            get_balance_callback = (self.executor.get_wallet_usdc_balance
                                    if mode == "live" else None),
        )

        # 状态
        self._open_trades:   dict[int, TradeRecord] = {}
        self._market_cache:  dict[str, object]      = {}
        self._running = False
        self._last_window_ts: Optional[int] = None
        # 风控通知去重：记录已通知过的原因，避免每5s重复推送
        self._risk_notified: set[str] = set()

        # 价格状态（跨 tick 共享）
        self._cl_last_price:     Optional[float] = None
        self._cl_last_oracle_ts: Optional[int]   = None
        self._cl_ptb_cache:      dict[int, float] = {}
        self._cc_ptb_cache:      dict[int, float] = {}
        self._bn_ptb_cache:      dict[int, float] = {}
        self._ptb_delay_cache:   dict[int, int]   = {}

        # CLOB 实时赔率状态
        self._last_clob_up: Optional[float] = None
        self._last_clob_dn: Optional[float] = None

    # ── 生命周期 ──────────────────────────────────────

    async def start(self):
        self._running = True
        logger.info("Bot 启动 | 模式=%s | 资金=%.2f", self.mode, self.initial_capital)
        notifier.notify(notifier.system_start(self.mode, self.initial_capital))

        issues = cfg.validate()
        if issues:
            for issue in issues:
                logger.error("配置错误: %s", issue)
            sys.exit(1)

        async with asyncio.TaskGroup() as tg:
            tg.create_task(self.price_feed.run(),     name="price_feed")
            tg.create_task(notifier.run(),            name="telegram")
            tg.create_task(self._main_loop(),         name="main_loop")
            tg.create_task(self._daily_reset_loop(),  name="daily_reset")
            tg.create_task(self._settlement_loop(), name="settlement")

    def stop(self, reason: str = "手动停止"):
        self._running = False
        self.price_feed.stop()
        stats     = self.risk.stats
        total_pnl = stats["pnl"]
        logger.info("Bot 停止 | %s | 总 PnL=%.2f", reason, total_pnl)
        notifier.notify(notifier.system_stop(reason, total_pnl))

    # ── 主循环 ─────────────────────────────────────────

    async def _main_loop(self):
        logger.info("主循环启动，轮询间隔 %ds", cfg.poll_interval_secs)
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
        now       = int(time.time())
        window_ts = current_window_ts()
        elapsed   = seconds_into_window()
        minute    = elapsed // 60
        ts_str    = datetime.now(timezone.utc).strftime("%H:%M:%S")

        # ── 1. BTC 价格（三源） ──
        loop      = asyncio.get_event_loop()
        btc_price = self.price_feed.price or await self.price_cache.get_async()
        if btc_price is None:
            logger.warning("[%s] 无法获取 BTC 价格，跳过", ts_str)
            return

        cc_price = await loop.run_in_executor(None, get_cryptocompare_price)

        # Chainlink 链上（轻量 RPC 调用）
        cl_price, cl_oracle_ts = await loop.run_in_executor(
            None, get_chainlink_onchain_price
        )
        if cl_price is not None:
            self._cl_last_price     = cl_price
            self._cl_last_oracle_ts = cl_oracle_ts
        else:
            cl_price     = self._cl_last_price
            cl_oracle_ts = self._cl_last_oracle_ts

        cl_age   = int(now - cl_oracle_ts) if cl_oracle_ts else None
        cl_fresh = cl_age is not None and cl_age < CL_FRESH_SECS

        # ── 2. 新窗口开盘 ──
        if window_ts != self._last_window_ts:
            await self._on_new_window(window_ts, btc_price, cc_price, cl_price, cl_fresh, elapsed)
            self._last_window_ts = window_ts
            self._last_clob_up   = None
            self._last_clob_dn   = None

        # ── 3. 三源 gap 计算 ──
        bn_ptb = self._bn_ptb_cache.get(window_ts, btc_price)
        cc_ptb = self._cc_ptb_cache.get(window_ts, cc_price or btc_price)
        cl_ptb = self._cl_ptb_cache.get(window_ts)

        bn_gap_pct = (btc_price - bn_ptb) / bn_ptb * 100
        cc_gap_pct = ((cc_price - cc_ptb) / cc_ptb * 100) if cc_price and cc_ptb else bn_gap_pct
        cl_gap_pct = None
        if cl_price and cl_fresh:
            ref_ptb = cl_ptb or cc_ptb
            if ref_ptb:
                cl_gap_pct = (cl_price - ref_ptb) / ref_ptb * 100

        # 主 gap 优先级：CL链上(新鲜) > CC > BN
        primary_gap = (cl_gap_pct if cl_gap_pct is not None and cl_fresh
                       else cc_gap_pct if cc_price else bn_gap_pct)

        # ── 4. Gamma 静态信息（每窗口一次） ──
        market = await self._get_market(window_ts)
        if market is None:
            logger.warning("[%s] ⚠️ 市场数据获取失败", ts_str)
            return

        # ── 5. CLOB 实时赔率（每 poll 调用） ──
        clob_up, clob_dn = await loop.run_in_executor(
            None, get_clob_midpoints, market.up_token, market.down_token
        )
        if clob_up is not None and clob_dn is not None:
            self._last_clob_up, self._last_clob_dn = clob_up, clob_dn
        else:
            clob_up = self._last_clob_up if self._last_clob_up is not None else market.gamma_up
            clob_dn = self._last_clob_dn if self._last_clob_dn is not None else market.gamma_dn
            if self._last_clob_up is None:
                logger.warning("[%s] ⚠️ CLOB 赔率获取失败，使用 Gamma 初始值", ts_str)

        market.update_clob_odds(clob_up, clob_dn)

        # 赔率跳变检测
        self.strategy.update_odds_history(window_ts, clob_up)

        # ── 6. 同步资金 ──
        # Paper 模式：资金上限 = 初始资金 × paper_capital_multiplier
        # 避免 Kelly 复利无限膨胀导致注额失真，影响策略分析的参考价值
        if self.mode == "paper":
            paper_cap = self.initial_capital * cfg.paper_capital_multiplier
            capped_capital = min(self.risk.current_capital, paper_cap)
            self.strategy.update_capital(capped_capital)
        else:
            self.strategy.update_capital(self.risk.current_capital)

        # ── 7. 预计算历史背景置信度（供路径2早期拦截使用）──
        mc_conf_up  = 1.0
        mc_conf_dn  = 1.0
        mc_ctx_note = ""
        if self.mc is not None:
            try:
                res_up     = self.mc.signal_confidence("UP",   primary_gap, minute)
                res_dn     = self.mc.signal_confidence("DOWN", primary_gap, minute)
                # 策略只用「原始」可信度，不再做 0.55 抬升：
                # 抬升会让分2 逆势 Down（如 2026-03-21 实盘 #326/#327）绕过 min_conf=0.35，放大亏损。
                mc_conf_up = res_up["score"]
                mc_conf_dn = res_dn["score"]

                # 心跳日志用：取赔率主导方向的上下文
                dominant_note_dir = "UP" if clob_up >= clob_dn else "DOWN"
                dominant_res  = res_up if dominant_note_dir == "UP" else res_dn
                mc_ctx_note   = (f" [{dominant_res['note']} 可信={dominant_res['score']:.2f}]"
                                 if dominant_res.get("note") else "")
            except Exception as e:
                logger.debug("MarketContext 置信度计算失败: %s", e)

        # ── 8. 心跳日志 ──
        gap_arrow = "↑" if primary_gap > 0 else ("↓" if primary_gap < 0 else "─")
        gap_src   = "CL" if cl_gap_pct is not None and cl_fresh else "CC" if cc_price else "BN"
        cl_tag    = f"CL=${cl_price:,.0f}({cl_age}s)" if cl_price else "CL=N/A"
        logger.info(
            "[%s] 分%d | BTC=$%.1f %s %s%+.3f%%(%s) | Up=%.2f Down=%.2f | cap=$%.2f%s",
            ts_str, minute, btc_price, cl_tag,
            gap_arrow, primary_gap, gap_src,
            clob_up, clob_dn, self.risk.current_capital,
            mc_ctx_note,
        )

        dashboard.maybe_print(btc_price, primary_gap, window_ts,
                              self.risk.current_capital, self.risk.stats, self.mode)

        # ── 9. 入场窗口检查 ──
        # entry_window_start=60(分1)，策略内部对各路径有独立时机约束
        if not (cfg.entry_window_start <= elapsed <= cfg.entry_window_end):
            return

        # ── 10. 风控检查 ──
        allowed, reason = self.risk.allow_trade()
        if not allowed:
            logger.warning("风控拦截: %s", reason)
            # 同一原因只推送一次，窗口切换后重置（不频繁刷 Telegram）
            notify_key = reason[:30]
            if notify_key not in self._risk_notified:
                self._risk_notified.add(notify_key)
                notifier.notify(notifier.risk_alert(
                    f"{reason}\n"
                    f"账户资金: <b>${self.risk.current_capital:.2f}</b>  "
                    f"已结算: {self.risk.stats['trades']}笔  "
                    f"胜率: {self.risk.win_rate:.1%}"
                ))
            return

        # 风控通过时，清除同类通知记录（允许恢复后重新通知）
        self._risk_notified.clear()

        # ── 11. 订单簿（仅路径3大gap时需要） ──
        orderbook = None
        if abs(primary_gap) >= cfg.min_gap_pct:
            direction_token = market.up_token if primary_gap > 0 else market.down_token
            orderbook = await loop.run_in_executor(None, get_orderbook, direction_token)

        ptb_delay = self._ptb_delay_cache.get(window_ts, 0)

        # 取赔率主导方向的置信度传入策略（路径2用于早期分钟拦截）
        dominant_dir       = "UP" if clob_up >= clob_dn else "DOWN"
        signal_confidence  = mc_conf_up if dominant_dir == "UP" else mc_conf_dn

        # 提取波动率等级（用于路径2优化）
        vol_level = "medium"
        if self.mc is not None:
            try:
                ctx = self.mc.get_context()
                if ctx:
                    vol_level = ctx.get("vol_level", "medium")
            except Exception:
                pass

        # ── 12. 评估信号 ──
        signal = self.strategy.evaluate(
            window_ts          = window_ts,
            btc_price          = btc_price,
            market             = market,
            orderbook          = orderbook,
            seconds_elapsed    = elapsed,
            cl_gap             = cl_gap_pct,
            cl_age             = cl_age,
            bn_gap             = bn_gap_pct,
            ptb_delay_secs     = ptb_delay,
            signal_confidence  = signal_confidence,
            vol_level          = vol_level,
        )

        if signal is None or not signal.is_valid:
            return

        # 附加精确上下文标签到 signal.note（信息性）
        if self.mc is not None:
            try:
                conf = self.mc.signal_confidence(
                    signal.direction.value, primary_gap, minute
                )
                rec  = conf.get("recommended_gap_threshold", 0)
                rec_str  = f" 建议gap≥{rec:.2f}%" if rec > 0.05 else ""
                note_tag = f"[{conf['note']} 可信={conf['score']:.2f}{rec_str}]"
                signal.note = (signal.note + " " + note_tag).strip()
            except Exception:
                pass

        # ── 13. 实盘注额修正：以链上余额为准，并应用 MAX_BET_USDC 硬上限 ──
        # 原因：Kelly 复利会让内存资金虚涨，导致注额远超实际钱包余额（详见实盘分析报告）
        if self.mode == "live":
            try:
                chain_bal = self.executor.get_wallet_usdc_balance()
                if chain_bal > 0:
                    # 可用资金 = min(内存资金, 链上余额) × max_bet_fraction
                    effective_capital = min(self.risk.current_capital, chain_bal)
                    kelly_bet = effective_capital * signal.kelly_fraction
                    # MAX_BET_USDC 硬上限
                    if cfg.max_bet_usdc > 0:
                        kelly_bet = min(kelly_bet, cfg.max_bet_usdc)
                    if kelly_bet < signal.bet_amount * 0.5:
                        logger.warning(
                            "注额修正: $%.2f → $%.2f (链上余额=%.2f USDC.e, 内存资金=%.2f)",
                            signal.bet_amount, kelly_bet, chain_bal, self.risk.current_capital,
                        )
                    signal.bet_amount = max(kelly_bet, 1.0)
            except Exception as e:
                logger.debug("链上余额查询失败，使用内存资金: %s", e)
                # 兜底：仅应用 MAX_BET_USDC 硬上限
                if cfg.max_bet_usdc > 0:
                    signal.bet_amount = min(signal.bet_amount, cfg.max_bet_usdc)

        # ── 14. 执行下单 ──
        trade = await self.executor.place(signal, window_ts)
        if trade is None:
            logger.error("下单失败")
            return

        self._open_trades[window_ts] = trade
        self.strategy.mark_traded(window_ts, signal.direction)
        logger.info(
            "✅ 下单成功 | %s | bet=$%.2f | 赔率=%.3f | EV=%+.3f | gap=%+.3f%%(%s) | %s",
            signal.direction.value, signal.bet_amount,
            signal.market_price, signal.ev_per_unit,
            signal.gap_pct, signal.gap_src, signal.note,
        )
        # 查链上余额（实盘才查，避免不必要的网络请求）
        wallet_usdc = 0.0
        if self.mode == "live":
            try:
                wallet_usdc = self.executor.get_wallet_usdc_balance()
            except Exception:
                pass
        notifier.notify(notifier.trade_opened(
            self.mode, signal.direction.value, trade.amount_usdc,
            signal.market_price, signal.ev_per_unit, signal.gap_pct,
            capital=self.risk.current_capital, wallet_usdc=wallet_usdc,
        ))

    # ── 每日重置循环 ──────────────────────────────────

    async def _daily_reset_loop(self):
        """
        每天 23:59:59（北京时间）触发每日重置：
          1. 发送日报（交易统计 + 胜率 + PnL）
          2. 从链上读取最新 USDC.e 余额作为新一天的起始资金
          3. 重置胜率、连续亏损计数、熔断状态
          4. 若链上余额 < $5，发送余额不足告警
        """
        LOW_BALANCE_THRESHOLD = 5.0  # 余额低于此值发告警

        while self._running:
            # 计算距离今天 23:59:59 的剩余秒数（北京时间 = UTC+8）
            now_local = datetime.now()
            target    = now_local.replace(hour=23, minute=59, second=59, microsecond=0)
            if now_local >= target:
                # 已过今天的目标时间，等到明天
                target = target.replace(day=target.day + 1)
            secs_to_reset = (target - now_local).total_seconds()

            logger.debug("每日重置倒计时: %.0f 秒（%s）", secs_to_reset, target.strftime("%m-%d %H:%M:%S"))
            await asyncio.sleep(secs_to_reset)

            if not self._running:
                break

            # ── 执行每日重置 ──
            now_str   = now_local.strftime("%Y-%m-%d")
            prev_stats = self.risk.stats

            # 1. 获取链上最新余额（实盘查链上；paper 用当前资金）
            loop = asyncio.get_event_loop()
            if self.mode == "live" and cfg.has_wallet:
                new_capital = await loop.run_in_executor(
                    None, self.executor.get_wallet_usdc_balance
                )
                if new_capital <= 0:
                    new_capital = self.risk.current_capital
                    logger.warning("每日重置：链上余额查询失败，沿用当前资金 $%.2f", new_capital)
            else:
                new_capital = self.risk.current_capital

            # 2. 发送日报
            notifier.notify(notifier.daily_summary(
                date        = now_str,
                trades      = prev_stats["trades"],
                wins        = prev_stats["wins"],
                pnl         = prev_stats["pnl"],
                new_capital = new_capital,
            ))
            logger.info(
                "📊 日报 %s | 交易=%d 胜=%d 胜率=%.1f%% PnL=%+.2f | 新余额=$%.2f",
                now_str, prev_stats["trades"], prev_stats["wins"],
                self.risk.win_rate * 100, prev_stats["pnl"], new_capital,
            )

            # 3. 重置风控统计
            self.risk.daily_reset(new_capital)
            self.strategy.update_capital(new_capital)
            self._risk_notified.clear()  # 清除风控通知去重记录
            logger.info("✅ 每日重置完成 | 新资金=$%.2f | 胜率归零", new_capital)

            # 4. 余额不足告警
            if new_capital < LOW_BALANCE_THRESHOLD:
                logger.warning("⚠️ 钱包余额 $%.2f < $%.0f 阈值，发送告警",
                               new_capital, LOW_BALANCE_THRESHOLD)
                notifier.notify(notifier.low_balance_alert(new_capital, LOW_BALANCE_THRESHOLD))

            # 重置后等待 2 秒，避免跨越零点时重复触发
            await asyncio.sleep(2)

    # ── 结算循环 ──────────────────────────────────────

    async def _settlement_loop(self):
        _hb            = 0
        _last_redeem_t = 0
        while self._running:
            await asyncio.sleep(30)
            _hb += 1
            if len(self._open_trades) > 0 or _hb % 10 == 0:
                logger.debug("结算检查 | 待结算=%d | PnL=%+.2f",
                             len(self._open_trades), self.risk.stats["pnl"])
            await self._settle_closed_windows()

            # 每5分钟扫描一次全部可兑换仓位（兜底，防止漏兑换）
            now = int(time.time())
            if self.redeemer.available and now - _last_redeem_t >= 300:
                _last_redeem_t = now
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self.redeemer.redeem_all)

    async def _settle_closed_windows(self):
        now = int(time.time())
        to_settle = [wts for wts in list(self._open_trades)
                     if now > wts + 310]
        loop = asyncio.get_event_loop()

        for wts in to_settle:
            trade  = self._open_trades.pop(wts)
            market = await loop.run_in_executor(None, get_market_info, wts)
            if market is None or not market.closed:
                self._open_trades[wts] = trade  # 放回重试
                continue

            result = ("Up"   if market.gamma_up >= 0.99 else
                      "Down" if market.gamma_dn >= 0.99 else
                      "Up"   if market.gamma_up >  0.5  else "Down")

            pnl = await self.executor.settle(trade, result)
            win = (result == trade.direction)
            self.risk.record_result(win, pnl)
            stats = self.risk.stats
            logger.info(
                "%s 结算 | %s → %s | PnL=%+.2f | 胜率=%.1f%% | 总PnL=%+.2f",
                "✅" if win else "❌",
                trade.direction, result, pnl,
                self.risk.win_rate * 100, stats["pnl"],
            )

            # 结算后立即检查是否触发风控阈值，若是则推送专项告警
            allowed_after, risk_reason = self.risk.allow_trade()
            if not allowed_after:
                notifier.notify(notifier.risk_alert(
                    f"{risk_reason}\n"
                    f"账户资金: <b>${stats['capital']:.2f}</b>  "
                    f"累计PnL: <b>{stats['pnl']:+.2f}</b>\n"
                    f"交易: {stats['trades']}笔  胜率: {self.risk.win_rate:.1%}"
                ))
                # 加入已通知集合，避免后续每5s重复推送
                self._risk_notified.add(risk_reason[:30])
            # 查链上余额（实盘才查）
            wallet_usdc_settle = 0.0
            if self.mode == "live":
                try:
                    wallet_usdc_settle = self.executor.get_wallet_usdc_balance()
                except Exception:
                    pass
            notifier.notify(notifier.trade_settled(
                self.mode, trade.direction, result,
                pnl, self.risk.win_rate, self.risk.stats["pnl"],
                capital=self.risk.current_capital, wallet_usdc=wallet_usdc_settle,
            ))

            # ── 自动兑换（实盘模式 + 赢单 + redeemer 可用）──
            if win and self.mode == "live" and self.redeemer.available:
                outcome_index = 0 if result == "Up" else 1
                condition_id  = getattr(trade, "condition_id", None) or market.condition_id
                size          = getattr(trade, "size", 0.0)
                neg_risk      = getattr(market, "neg_risk", False)
                logger.info("触发自动兑换: condition=%s outcome=%d size=%.4f",
                            condition_id[:12] + "…", outcome_index, size)
                redeem_result = await loop.run_in_executor(
                    None, self.redeemer.redeem_one,
                    condition_id, outcome_index, size, neg_risk,
                )
                if redeem_result.success:
                    logger.info("✅ 自动兑换成功 tx=%s", str(redeem_result.tx_hash)[:20] + "…")
                    # 兑换成功后检查是否触发资金归集
                    if cfg.has_sweep:
                        await self._maybe_sweep(loop)
                else:
                    logger.warning("⚠️ 自动兑换失败（已加入待兑换队列）: %s", redeem_result.error)

    # ── 辅助方法 ─────────────────────────────────────

    async def _maybe_sweep(self, loop):
        """兑换成功后检查余额，超过阈值则执行资金归集。"""
        try:
            balance = await loop.run_in_executor(
                None, self.executor.get_wallet_usdc_balance
            )
            if balance <= cfg.sweep_threshold:
                logger.debug(
                    "归集检查: 余额=$%.2f 未超过阈值=$%.2f，跳过",
                    balance, cfg.sweep_threshold,
                )
                return
            logger.info(
                "💸 触发资金归集: 余额=$%.2f 阈值=$%.2f 比例=%.0f%%",
                balance, cfg.sweep_threshold, cfg.sweep_ratio * 100,
            )
            ok, sent, tx = await loop.run_in_executor(
                None, self.executor.sweep_usdc, balance
            )
            notifier.notify(notifier.sweep_result(ok, sent, balance, cfg.sweep_wallet, tx))
            if ok:
                # 归集后同步资金基准，避免 Kelly 仓位基于归集前余额计算
                # 同时更新 _day_start_capital 防止 day_loss 虚高触发熔断
                post_sweep = balance - sent
                self.risk.sweep_capital(sent)
                self.strategy.update_capital(post_sweep)
                logger.info("归集后资金更新: $%.2f → $%.2f", balance, post_sweep)
        except Exception as e:
            logger.warning("资金归集检查异常: %s", e)

    async def _on_new_window(
        self, window_ts: int, btc_price: float,
        cc_price: Optional[float], cl_price: Optional[float],
        cl_fresh: bool, elapsed: int,
    ):
        """窗口开盘初始化：记录 PtB（优先 Chainlink链上 > CC > BN）"""
        self._bn_ptb_cache[window_ts] = btc_price
        self._cc_ptb_cache[window_ts] = cc_price or btc_price
        if cl_price and cl_fresh:
            self._cl_ptb_cache[window_ts] = cl_price
        self._ptb_delay_cache[window_ts] = elapsed

        if cl_price and cl_fresh:
            ptb, ptb_src = cl_price, f"CL链上PtB=${cl_price:,.2f}"
        elif cc_price:
            ptb, ptb_src = cc_price, f"CC估算PtB=${cc_price:,.2f}"
        else:
            ptb, ptb_src = btc_price, f"BN估算PtB=${btc_price:,.2f}"

        quality = ("精确" if elapsed <= cfg.poll_interval_secs else
                   f"轻微延迟({elapsed}s)" if elapsed <= 30 else
                   f"⚠️延迟较大({elapsed}s)")
        logger.info("── 新窗口 ts=%d  %s  [%s]  资金=$%.2f ──",
                    window_ts, ptb_src, quality, self.risk.current_capital)

        self.strategy.set_price_to_beat(window_ts, ptb)

        # 历史背景预取（异步，不阻塞主循环）
        if self.mc is not None:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.mc.refresh, True)

        # 清理旧缓存（保留最近 2 个窗口）
        old = window_ts - 600
        for cache in (self._bn_ptb_cache, self._cc_ptb_cache,
                      self._cl_ptb_cache, self._ptb_delay_cache):
            for k in list(cache):
                if k < old:
                    del cache[k]

    async def _get_market(self, window_ts: int) -> Optional[object]:
        """Gamma 静态信息（每窗口仅一次）"""
        key = str(window_ts)
        if key in self._market_cache:
            return self._market_cache[key]
        loop   = asyncio.get_event_loop()
        market = await loop.run_in_executor(None, get_market_info, window_ts)
        if market:
            self._market_cache[key] = market
            old = str(window_ts - 600)
            for k in [k for k in self._market_cache if k < old]:
                del self._market_cache[k]
        return market


# ─────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Polymarket BTC 末期套利机器人")
    parser.add_argument("--mode",    choices=["paper", "live"], default=cfg.mode)
    parser.add_argument("--capital", type=float, default=None,
                        help="初始资金（paper模式默认1000；live模式默认自动读取链上USDC.e余额）")
    parser.add_argument("--debug",   action="store_true")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="跳过实盘启动确认（批量部署时使用）")
    return parser.parse_args()


def _fetch_wallet_usdc_balance() -> float:
    """从 Polygon 链上读取钱包 USDC.e 余额，失败返回 0.0。"""
    try:
        from web3 import Web3
        USDC_E = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174'
        ERC20_ABI = [{"name": "balanceOf", "type": "function", "stateMutability": "view",
                      "inputs": [{"name": "account", "type": "address"}],
                      "outputs": [{"name": "", "type": "uint256"}]}]
        proxy = os.environ.get("PROXY_URL", "").strip()
        rpc_kwargs: dict = {"timeout": 8}
        if proxy:
            rpc_kwargs["proxies"] = {"http": proxy, "https": proxy}
        w3 = Web3(Web3.HTTPProvider("https://polygon-bor.publicnode.com",
                                    request_kwargs=rpc_kwargs))
        usdc = w3.eth.contract(
            address=Web3.to_checksum_address(USDC_E), abi=ERC20_ABI
        )
        bal = usdc.functions.balanceOf(
            Web3.to_checksum_address(cfg.wallet_address)
        ).call()
        return bal / 1e6  # USDC.e 6位小数
    except Exception as e:
        logger.warning("链上余额查询失败: %s", e)
        return 0.0


async def _run(mode: str, capital: float):
    bot  = PolymarketBot(mode=mode, initial_capital=capital)
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

    # ── 资金确定 ──
    # live 模式：自动从链上读取 USDC.e 余额（可用 --capital 手动覆盖）
    # paper 模式：默认 $1000（可用 --capital 指定）
    if args.capital is not None:
        capital = args.capital
        capital_src = "手动指定"
    elif mode == "live" and cfg.has_wallet:
        capital = _fetch_wallet_usdc_balance()
        capital_src = "链上余额"
        if capital <= 0:
            logger.error("链上 USDC.e 余额为 0 或查询失败，请手动指定 --capital 或充值钱包")
            sys.exit(1)
    else:
        capital = 1000.0
        capital_src = "默认值"

    logger.info("=" * 60)
    logger.info("  Polymarket BTC 末期套利 Bot  v3")
    logger.info("  模式: %s  资金: $%.2f (%s)", mode.upper(), capital, capital_src)
    logger.info("  价格源: Chainlink链上 > CryptoCompare > Binance")
    logger.info("  赔率源: CLOB 实时订单簿(每%ds) > Gamma 初始值(备用)",
                cfg.poll_interval_secs)
    if _HAS_MC:
        logger.info("  历史背景: MarketContext（90m趋势/波动率，参与信号过滤）")
    else:
        logger.info("  历史背景: 未加载（MarketContext 不可用）")
    gp = cfg.greed_params
    logger.info("  信号路径: 路径1(跟赔率) | 路径2(赔率强信号) | 路径3(末期套利)")
    logger.info("  贪婪指数: %d/10 | 赔率下限: %.2f | gap下限: %.2f%% | EV下限: %.2f",
                cfg.greed_index, gp["min_odds_path2"], gp["min_gap"], gp["min_ev"])
    logger.info("=" * 60)

    if mode == "live":
        # 预检：实盘依赖（用 find_spec 只检查包是否存在，避免 import 执行时的副作用）
        import importlib.util as _ilu
        if _ilu.find_spec("py_clob_client") is None:
            logger.error(
                "实盘模式需要 py-clob-client，当前 Python 未安装。\n"
                "  安装命令：\n"
                "    pip3 install --break-system-packages py-clob-client\n"
                "  或在虚拟环境中：\n"
                "    .venv/bin/pip install py-clob-client"
            )
            sys.exit(1)

        if not cfg.has_wallet:
            logger.error("实盘模式需要在 .env 中配置 PRIVATE_KEY 和 WALLET_ADDRESS")
            sys.exit(1)
        logger.warning("⚠️  实盘模式：将使用真实资金！钱包=%s  资金=$%.2f",
                       cfg.wallet_address[:10] + "…", capital)
        if not args.yes:
            try:
                confirm = input("输入 YES 确认启动实盘（或加 --yes 跳过此步骤）: ").strip()
                if confirm != "YES":
                    logger.info("已取消")
                    sys.exit(0)
            except (EOFError, KeyboardInterrupt):
                sys.exit(0)
        else:
            logger.info("✅ --yes 模式，跳过确认，直接启动")

    asyncio.run(_run(mode, capital))


if __name__ == "__main__":
    main()
