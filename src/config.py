"""
全局配置：从 .env 文件或环境变量读取，提供类型安全的参数访问
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

# 自动加载项目根目录的 .env 文件
_root = Path(__file__).parent.parent
_env_path = _root / ".env"
if _env_path.exists():
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                # 去掉行内注释（# 及其后内容），再去除首尾空白
                v = v.split("#")[0].strip()
                os.environ.setdefault(k.strip(), v)


@dataclass(frozen=True)
class Config:
    # ── 区块链 / Polymarket ──
    private_key: str      = field(default_factory=lambda: os.getenv("PRIVATE_KEY", ""))
    wallet_address: str   = field(default_factory=lambda: os.getenv("WALLET_ADDRESS", ""))
    polymarket_host: str  = field(default_factory=lambda: os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com"))
    gamma_host: str       = "https://gamma-api.polymarket.com"
    chain_id: int         = field(default_factory=lambda: int(os.getenv("CHAIN_ID", "137")))

    # ── Telegram ──
    telegram_token: str   = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    # 机器别名：多机部署时用于区分消息来源，显示在每条 Telegram 通知顶部
    # 建议设置为易识别的名称，如 "Mac-主力" / "云服务器-备用" / "VPS-A"
    # 留空则自动使用钱包地址后6位
    bot_alias: str        = field(default_factory=lambda: os.getenv("BOT_ALIAS", ""))

    # ── 运行模式 ──
    mode: str             = field(default_factory=lambda: os.getenv("MODE", "paper"))

    # ── 策略参数 ──
    min_gap_pct: float    = field(default_factory=lambda: float(os.getenv("MIN_GAP_PCT", "0.10")))
    entry_margin: float   = field(default_factory=lambda: float(os.getenv("ENTRY_MARGIN", "0.03")))
    min_ev_threshold: float = field(default_factory=lambda: float(os.getenv("MIN_EV_THRESHOLD", "0.08")))
    # 入场窗口：下限 60s（分1）以允许路径2早期赔率信号；上限 270s（4:30）
    # 策略内部对各路径有独立时机约束（路径2 minute>=1，路径1/3 minute>=3）
    entry_window_start: int = 60    # 1 分钟
    entry_window_end: int   = 270   # 4 分 30 秒

    # ── 风险控制 ──
    max_bet_fraction: float       = field(default_factory=lambda: float(os.getenv("MAX_BET_FRACTION", "0.05")))
    # 实盘单笔最大注额（USDC 硬上限，0=不限）
    # 实盘分析：Kelly 复利会让内存资金虚涨，导致单笔注额远超实际钱包余额，设置硬上限防止失控
    max_bet_usdc: float           = field(default_factory=lambda: float(os.getenv("MAX_BET_USDC", "0")))
    # 低赔率机会（market_price < LOW_ODDS_THRESH）允许更高的 Kelly 上限
    # 数据验证：entry_price < 0.55 时实际胜率 96.4%，可适当提高仓位
    low_odds_thresh: float        = 0.55
    high_conf_bet_fraction: float = field(default_factory=lambda: float(os.getenv("HIGH_CONF_BET_FRACTION", "0.25")))
    # Paper 模式资金上限 = 初始资金 × paper_capital_multiplier，避免复利失控导致数据失真
    paper_capital_multiplier: float = 3.0
    max_daily_loss_fraction: float = field(default_factory=lambda: float(os.getenv("MAX_DAILY_LOSS_FRACTION", "0.15")))
    max_consecutive_losses: int   = 5
    pause_after_loss_minutes: int = 60

    # ── 数据采集 ──
    poll_interval_secs: int = 5
    db_path: str            = field(default_factory=lambda: str(_root / "data" / "observations.db"))
    log_dir: str            = field(default_factory=lambda: str(_root / "logs"))

    # ── 理论胜率表（基于7天回测，第4分钟，gap绝对值 → 理论胜率）──
    WIN_RATE_TABLE: tuple = (
        (0.30, 0.995),
        (0.20, 0.982),
        (0.15, 0.979),
        (0.10, 0.968),
        (0.05, 0.897),
    )

    def theoretical_win_rate(self, gap_abs_pct: float, minute_in_window: int) -> float:
        """根据当前 gap 和分钟数查表得出理论胜率"""
        if minute_in_window < 3:
            return 0.5
        for threshold, win_rate in self.WIN_RATE_TABLE:
            if gap_abs_pct >= threshold:
                return win_rate
        return 0.5

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def has_wallet(self) -> bool:
        return bool(self.private_key and self.wallet_address)

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    def validate(self) -> list[str]:
        """返回配置问题列表，空列表表示配置完整"""
        issues = []
        if self.is_live and not self.has_wallet:
            issues.append("实盘模式需要设置 PRIVATE_KEY 和 WALLET_ADDRESS")
        return issues


# 全局单例
cfg = Config()
