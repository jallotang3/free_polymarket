"""
监控与告警模块

功能：
  1. 结构化日志（文件 + 控制台，含颜色）
  2. Telegram 消息推送（可选）
  3. 实时控制台状态面板
"""

import asyncio
import json
import logging
import os
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# 强制所有日志时间戳使用 UTC（避免与 Polymarket ET 时间混淆）
logging.Formatter.converter = time.gmtime

from .config import cfg

# ─────────────────────────────────────────────
# 日志初始化
# ─────────────────────────────────────────────

_ANSI = {
    "reset": "\033[0m",
    "bold":  "\033[1m",
    "green": "\033[92m",
    "red":   "\033[91m",
    "yellow":"\033[93m",
    "cyan":  "\033[96m",
    "dim":   "\033[2m",
}


class ColorFormatter(logging.Formatter):
    LEVEL_COLORS = {
        logging.DEBUG:    _ANSI["dim"],
        logging.INFO:     _ANSI["cyan"],
        logging.WARNING:  _ANSI["yellow"],
        logging.ERROR:    _ANSI["red"],
        logging.CRITICAL: _ANSI["red"] + _ANSI["bold"],
    }

    def format(self, record: logging.LogRecord) -> str:
        color = self.LEVEL_COLORS.get(record.levelno, "")
        reset = _ANSI["reset"]
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        name = record.name.split(".")[-1][:10].ljust(10)
        msg = super().format(record)
        # 只对 INFO+ 的消息加颜色
        if record.levelno >= logging.INFO:
            return f"{_ANSI['dim']}{ts}{reset} {color}{name}{reset} {msg}"
        return f"{_ANSI['dim']}{ts} {name} {msg}{reset}"


def setup_logging(level: int = logging.INFO):
    log_dir = Path(cfg.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    log_file = log_dir / f"bot_{today}.log"

    root = logging.getLogger()
    root.setLevel(level)

    # 控制台 handler（彩色）
    ch = logging.StreamHandler()
    ch.setFormatter(ColorFormatter())
    ch.setLevel(level)

    # 文件 handler（纯文本，强制 UTC）
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s UTC %(levelname)-8s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    fh.setLevel(logging.DEBUG)

    root.handlers.clear()
    root.addHandler(ch)
    root.addHandler(fh)

    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    logging.getLogger("monitor").info(
        "日志已初始化 | 级别=%s | 文件=%s",
        logging.getLevelName(level), log_file,
    )


# ─────────────────────────────────────────────
# Telegram 推送
# ─────────────────────────────────────────────

class TelegramNotifier:
    def __init__(self):
        self._token = cfg.telegram_token
        self._chat_id = cfg.telegram_chat_id
        self._enabled = cfg.has_telegram
        self._queue: asyncio.Queue = asyncio.Queue()
        self._logger = logging.getLogger("telegram")

        # 机器标识：用于多机部署时区分消息来源
        # 优先使用 BOT_ALIAS，否则自动用钱包地址后6位
        if cfg.bot_alias:
            self._alias = cfg.bot_alias
        elif cfg.wallet_address:
            self._alias = f"钱包…{cfg.wallet_address[-6:]}"
        else:
            self._alias = "Bot"

    def _header(self) -> str:
        """生成统一的机器标识头部，附加在每条消息最前面。"""
        return f"🖥 <code>{self._alias}</code>\n"

    async def run(self):
        """后台消费消息队列"""
        if not self._enabled:
            self._logger.info("Telegram 未配置，告警仅输出到日志")
            return
        while True:
            msg = await self._queue.get()
            await self._send(msg)
            self._queue.task_done()
            await asyncio.sleep(0.5)  # 避免触发 Telegram 频率限制

    def notify(self, text: str):
        """非阻塞入队，供各模块调用"""
        if self._enabled:
            try:
                self._queue.put_nowait(text)
            except asyncio.QueueFull:
                pass

    async def _send(self, text: str):
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = json.dumps({
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_notification": False,
        }).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, urllib.request.urlopen, req)
        except Exception as e:
            self._logger.debug("Telegram 发送失败: %s", e)

    # ── 格式化消息模板 ──

    def trade_opened(self, mode: str, direction: str, amount: float,
                     entry_price: float, ev: float, gap: float,
                     capital: float = 0.0, wallet_usdc: float = 0.0):
        emoji = "🟢" if mode == "live" else "📝"
        mode_str = "实盘" if mode == "live" else "纸面"
        bal_line = ""
        if mode == "live" and wallet_usdc > 0:
            bal_line = f"\n💰 钱包余额: <b>${wallet_usdc:.2f} USDC.e</b>"
        elif capital > 0:
            bal_line = f"\n💰 账户资金: <b>${capital:.2f}</b>"
        return (
            f"{self._header()}"
            f"{emoji} <b>{mode_str}下单</b>\n"
            f"方向: <b>{direction}</b>  金额: <b>${amount:.2f}</b>\n"
            f"入场价: {entry_price:.3f}  EV: {ev:+.4f}  gap: {gap:+.3f}%"
            f"{bal_line}"
        )

    def trade_settled(self, mode: str, direction: str, result: str,
                      pnl: float, win_rate: float, total_pnl: float,
                      capital: float = 0.0, wallet_usdc: float = 0.0):
        won = result == direction
        emoji = "✅" if won else "❌"
        mode_str = "实盘" if mode == "live" else "纸面"
        bal_line = ""
        if mode == "live" and wallet_usdc > 0:
            bal_line = f"\n💰 钱包余额: <b>${wallet_usdc:.2f} USDC.e</b>"
        elif capital > 0:
            bal_line = f"\n💰 账户资金: <b>${capital:.2f}</b>"
        return (
            f"{self._header()}"
            f"{emoji} <b>{mode_str}结算</b>\n"
            f"方向: {direction}  结果: <b>{result}</b>  PnL: <b>{pnl:+.2f} USDC</b>\n"
            f"累计 PnL: {total_pnl:+.2f}  历史胜率: {win_rate:.1%}"
            f"{bal_line}"
        )

    def daily_summary(self, date: str, trades: int, wins: int, pnl: float):
        wr = wins / trades if trades else 0
        emoji = "📈" if pnl > 0 else "📉"
        return (
            f"{self._header()}"
            f"{emoji} <b>日报 {date}</b>\n"
            f"交易: {trades}  胜: {wins}  胜率: {wr:.1%}\n"
            f"日收益: <b>{pnl:+.2f} USDC</b>"
        )

    def risk_alert(self, reason: str):
        return (
            f"{self._header()}"
            f"⚠️ <b>风控告警</b>\n{reason}"
        )

    def system_start(self, mode: str, capital: float):
        return (
            f"{self._header()}"
            f"🚀 <b>Bot 启动</b>\n"
            f"模式: {'实盘 🔴' if mode=='live' else '纸面 🟡'}\n"
            f"初始资金: ${capital:.2f}"
        )

    def system_stop(self, reason: str, final_pnl: float):
        return (
            f"{self._header()}"
            f"🛑 <b>Bot 停止</b>\n"
            f"原因: {reason}\n"
            f"最终 PnL: {final_pnl:+.2f} USDC"
        )


# ─────────────────────────────────────────────
# 控制台状态面板
# ─────────────────────────────────────────────

class Dashboard:
    """每隔一段时间向控制台输出当前状态摘要"""

    def __init__(self, interval_secs: int = 60):
        self._interval = interval_secs
        self._last_print = 0.0

    def maybe_print(
        self,
        btc_price: Optional[float],
        gap_pct: Optional[float],
        window_ts: int,
        capital: float,
        stats: dict,
        mode: str,
    ):
        now = time.time()
        if now - self._last_print < self._interval:
            return
        self._last_print = now

        elapsed = int(now) - window_ts
        remaining = max(0, 300 - elapsed)
        minute = elapsed // 60

        price_str = f"${btc_price:,.2f}" if btc_price else "N/A"
        gap_str = f"{gap_pct:+.3f}%" if gap_pct is not None else "N/A"
        pnl_color = _ANSI["green"] if stats["pnl"] >= 0 else _ANSI["red"]
        reset = _ANSI["reset"]

        print(
            f"\n{'─'*55}\n"
            f"  {_ANSI['bold']}Polymarket BTC Bot  [{mode.upper()}]{reset}\n"
            f"  时间: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}\n"
            f"  BTC:  {price_str}  gap={gap_str}\n"
            f"  窗口: 第{minute}分{elapsed%60}秒 / 剩余{remaining}s\n"
            f"  资金: ${capital:,.2f}  "
            f"PnL: {pnl_color}{stats['pnl']:+.2f}{reset}\n"
            f"  交易: {stats['trades']}笔  胜率: {stats['win_rate']:.1%}\n"
            f"{'─'*55}\n"
        )


# 全局单例
notifier = TelegramNotifier()
dashboard = Dashboard()
