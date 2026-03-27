# Polymarket BTC 末期套利机器人

基于 Polymarket BTC 5分钟涨跌预测市场的**末期定价套利**策略。

> 核心思路：在窗口第 3~4 分钟，当 BTC 价格已明显领先开盘价（gap ≥ 0.10%）时，
> 历史真实结算数据验证准确率高达 93.2~98.1%（基于464个窗口），
> 若 Polymarket 市场定价仍低于此概率，则存在套利空间。

---

## 快速启动

### 1. 安装依赖

**Python 版本：需要 3.12 及以上**（`polymarket-apis` 自动兑换依赖要求 ≥3.12）。  
Debian 12 默认 `apt install python3.12` **不可用**；**一键装环境**：运行 [debian/setup_env.sh](debian/setup_env.sh)（说明见 [debian/README.md](debian/README.md)）。手工步骤见 [doc/DEBIAN12_PYTHON312.md](doc/DEBIAN12_PYTHON312.md)。

```bash
pip install -r requirements.txt
# 或使用虚拟环境（推荐，且请用 python3.12 创建）
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

**Docker**：宿主机可不装 Python 3.12，用 Compose 构建并运行，见 [docker/README.md](docker/README.md)。

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，纸面交易模式 Telegram 以外的字段均可留空
```

### 3. Phase 1：数据采集（无需资金，建议运行 3 天）

```bash
python scripts/collect_data.py
```

采集完成后补填历史结算结果：
```bash
python scripts/backfill_results.py
```

### 4. Phase 2：纸面交易

```bash
./run_bot.sh --mode paper --capital 1000
# 或
python -m src.bot --mode paper --capital 1000
```

### 5. Phase 3：实盘

```bash
# 确保 .env 中配置了 PRIVATE_KEY 和 WALLET_ADDRESS
./run_bot.sh --mode live --capital 30
```

---

## 项目结构

```
fuck_polymarket/
├── src/
│   ├── config.py          # 全局配置（从 .env 读取）
│   ├── data_feed.py       # 数据源：Binance WebSocket + Polymarket CLOB + Chainlink
│   ├── strategy.py        # 策略引擎：三条信号路径 + 18因子过滤体系
│   ├── executor.py        # 下单执行（paper/live 双模式，含链上 USDC approve）
│   ├── redeemer.py        # 自动兑换获胜仓位（polymarket-apis）
│   ├── monitor.py         # Telegram 告警 + 彩色日志 + 状态面板
│   └── bot.py             # 主入口（asyncio 调度器）
├── scripts/
│   ├── collect_data.py    # Phase 1：采集赔率时序 + 自动记录结算结果
│   ├── backfill_results.py # 补填历史结算结果到 window_results 表
│   ├── market_context.py  # MarketContext：90分钟趋势/波动率计算
│   └── analyze_collected.py # 分析套利空间
├── data/
│   └── observations.db    # SQLite（observations / window_results / trades）
├── logs/                  # 按日期分割的日志文件
├── doc/                   # 分析报告与未解决问题文档
├── run_bot.sh             # 使用虚拟环境启动 bot 的脚本
├── .env.example           # 环境变量模板
└── requirements.txt
```

---

## 策略信号路径

Bot 有三条独立的信号触发路径：

| 路径 | 触发条件 | 适用场景 |
|------|---------|---------|
| **路径1（跟赔率）** | gap ≥ 0.05% + 分3+ + 赔率与 gap 反向 | 市场定价已反转，跟强势赔率 |
| **路径2（赔率强信号）** | gap < 0.05% + 赔率 ≥ 0.72 + 跳变/分3+ | 价格信号弱但赔率极强 |
| **路径3（末期套利）** | gap ≥ 0.10% + 分3+ | 价格偏差大，直接套利 |

---

## 决策因子体系（18个）

策略引擎综合使用以下 18 个因子进行信号过滤与仓位计算。

### 第一类：价格与 Gap 因子（4个）

| 因子 | 说明 | 来源 / 阈值 |
|------|------|-----------|
| `cl_gap` | Chainlink 链上价格 vs 窗口开盘 PtB 的偏差 | Polygon 链上预言机，主用 |
| `bn_gap` | Binance 实时价格 vs 开盘 PtB 的偏差 | 辅助对比，BN反向>0.08%时阻断 |
| `cl_age` | Chainlink 链上价格的新鲜度（秒） | < 45s 才使用；否则退回 Binance |
| `ptb_delay_secs` | 开盘 PtB 记录时的延迟 | > 60s 则 gap 信号失效 |

### 第二类：赔率因子（5个）

| 因子 | 说明 | 关键阈值 |
|------|------|---------|
| `up_odds / down_odds` | CLOB 实时买卖中间价（每5s更新） | 主导赔率 ≥ 0.72 才触发 |
| `odds_jumped` | 是否发生赔率跳变（单次变化 ≥ 0.10） | 是/否，影响早期分钟入场资格 |
| `jump_count` | 同向连续跳变次数 | ≥ 2 时冷却延长至 20s |
| `jump_ts` | 最近一次跳变的时间戳 | 单次跳变冷却 15s；连续跳变 20s |
| `soft_conflict` | 对立赔率 vs gap 方向的软冲突检测 | 对立赔率 > 0.65（跳变时 0.58）阻断 |

### 第三类：时间因子（1个）

| 因子 | 说明 | 关键阈值 |
|------|------|---------|
| `minute_in_window` | 当前处于 5 分钟窗口的第几分钟（0~4） | 分1/2/3/4 触发不同的过滤规则 |

**分钟级规则摘要：**
- **分1**：禁止 TWR=0.860 信号（odds < 0.85）；高赔率需 conf ≥ 0.55
- **分2 UP**：额外要求 price < 0.80 且 gap 同向 ≥ 0.15%
- **分3+**：所有信号路径均可触发；高赔率（>0.85）需 conf ≥ 0.60

### 第四类：历史背景因子（2个）

由 `scripts/market_context.py` 基于 90 分钟 Binance K 线计算，窗口开盘时刷新。

| 因子 | 说明 | 影响 |
|------|------|------|
| `signal_confidence` | 趋势方向与当前信号的一致性评分（0~1） | < 0.50 时 EV 门槛从 0.08 提高至 0.12 |
| `atr_30m` | 30 分钟平均真实波幅（波动率指标） | 高波动时动态调整推荐 gap 阈值 |

### 第五类：期望值与仓位因子（3个）

| 因子 | 计算公式 | 关键阈值 |
|------|---------|---------|
| `EV` | `TWR × (1 - mkt_price) - (1 - TWR) × mkt_price` | ≥ 0.08（低 conf 时 ≥ 0.12） |
| `kelly_fraction` | Half-Kelly：`0.5 × (b×p - q) / b` | 上限 10%（低赔率<0.55时25%） |
| `theo_win_rate (TWR)` | 按 gap 幅度查表（见下方） | 0.860 / 0.968 / ... |

**理论胜率查表（分3+）：**

| gap 绝对值 | 理论胜率 | 实测准确率（464窗口） |
|-----------|---------|-------------------|
| ≥ 0.30%  | 99.5%  | **100.0%** |
| ≥ 0.20%  | 98.2%  | **98.2%**  |
| ≥ 0.15%  | 97.9%  | **94.2%**  |
| ≥ 0.10%  | 96.8%  | **94.4%**  |
| 赔率路径   | 86.0% / 96.8% | 待积累更多实盘数据 |

### 第六类：风控因子（3个）

| 因子 | 说明 | 当前值 |
|------|------|-------|
| `max_bet_usdc` | 单笔最大注额（硬上限，防 Kelly 复利失控） | $3（.env 配置） |
| `max_daily_loss_fraction` | 日最大亏损率，超过则当日熔断 | 30% |
| `chain_balance` | 实盘下单前实时查询链上 USDC.e 余额 | 以实际余额 × fraction 为准 |

---

## 关键过滤规则（防亏损层）

基于实盘数据复盘建立的多层过滤，覆盖了所有已知亏损场景：

| 规则 | 覆盖的实盘亏损案例 |
|------|-----------------|
| 低 conf（<0.50）时 EV ≥ 0.12 | 04:38 Up@0.775 -$7.48（conf=0.42，EV=0.085） |
| 跳变后等待 15s 冷却 | 04:41 Down@0.880 -$7.92（跳变后12s下单） |
| 连续≥2次同向跳变后冷却延长至 20s | 05:47 Down@0.730 -$4.65（双跳变后26s下单） |
| 路径3 高赔率（>0.85）需 conf ≥ 0.60 | 05:43 Up@0.885 -$5.00（conf=0.52 震荡市） |
| 路径2 分3 高赔率双确认（gap + conf） | 04:41 Down@0.880 案例 |
| 分1 TWR=0.860 禁止入场 | Paper 数据分1亏损集中区 |

---

## 配置参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `MIN_EV_THRESHOLD` | 0.08 | 最低期望收益阈值 |
| `MIN_GAP_PCT` | 0.10 | 触发末期套利的最小 gap（%） |
| `ENTRY_MARGIN` | 0.03 | 理论胜率 - 市场赔率 的最小边际 |
| `MAX_BET_FRACTION` | 0.20 | 单笔最大仓位（总资金比例） |
| `MAX_BET_USDC` | 3 | 实盘单笔最大注额（USDC 硬上限） |
| `HIGH_CONF_BET_FRACTION` | 0.25 | 低赔率（<0.55）机会的 Kelly 上限 |
| `MAX_DAILY_LOSS_FRACTION` | 0.30 | 日最大亏损比例（触发熔断） |
| `PROXY_URL` | 空 | HTTP/SOCKS5 代理（可选） |

---

## 数据积累现状

| 数据 | 数量 | 说明 |
|------|------|------|
| 观测记录（observations） | 21,789 条 | 2026-03-17 ~ 至今，每5s一条 |
| 结算结果（window_results） | 464 个窗口 | 已验证真实 UP/DOWN 结果 |
| 实盘交易（live trades） | 17 笔 | 胜率 73.3%，净 PnL -$10.19 |
| Paper 交易 | 186 笔 | 胜率 88.2%，用于策略校准 |

---

## 风险提示

1. **本项目仅供学习研究**，不构成投资建议
2. 加密货币预测市场存在合规风险，请确认当地法规
3. 策略盈利高度依赖「市场定价低于真实胜率」这一条件，市场充分定价后套利空间会收窄
4. 实盘前务必完成纸面交易验证（100 笔以上）
5. 私钥安全至关重要，使用专用小额钱包，与主要资产完全隔离
6. 单笔注额建议不超过总资金的 10%，`MAX_BET_USDC` 建议设置为钱包余额的 10~15%
