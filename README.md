# Polymarket BTC 末期套利机器人

基于 Polymarket BTC 5分钟涨跌预测市场的**末期定价套利**策略。

> 核心思路：在窗口第 3:30~4:30 分钟，当 BTC 价格已明显领先开盘价（≥0.10%）时，
> 历史胜率高达 96.8%，若 Polymarket 市场定价仍低于此概率，则存在套利空间。

---

## 快速启动

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，纸面交易模式 Telegram 以外的字段均可留空
```

### 3. Phase 1：数据采集（无需资金，建议运行 3 天）

```bash
python scripts/collect_data.py
```

### 4. 分析采集数据

```bash
python scripts/analyze_collected.py
```

根据输出决定是否进入下一阶段：
- **EV > 0.03** → 进入 Phase 2
- **EV 0.01~0.03** → 继续观察
- **EV < 0.01** → 市场已充分定价，暂停

### 5. Phase 2：纸面交易

```bash
python -m src.bot --mode paper --capital 1000
```

### 6. Phase 3：实盘（谨慎）

```bash
# 确保 .env 中配置了 PRIVATE_KEY 和 WALLET_ADDRESS
python -m src.bot --mode live
```

---

## 项目结构

```
fuck_polymarket/
├── src/
│   ├── config.py          # 全局配置（从 .env 读取）
│   ├── data_feed.py       # 数据源：Binance WebSocket + Polymarket API
│   ├── strategy.py        # 末期套利策略引擎 + 风险控制器
│   ├── executor.py        # 下单执行（paper/live 双模式）
│   ├── monitor.py         # Telegram 告警 + 彩色日志 + 状态面板
│   └── bot.py             # 主入口（asyncio 调度器）
├── scripts/
│   ├── collect_data.py    # Phase 1：采集 Polymarket 赔率时序数据
│   ├── analyze_collected.py # Phase 1：分析套利空间
│   └── backtest.py        # 历史回测（基于 Binance 1分钟 K 线）
├── data/                  # SQLite 数据库（observations.db / bot.db）
├── logs/                  # 日志文件
├── doc/PLAN.md            # 完整实施方案和回测报告
├── .env.example           # 环境变量模板
└── requirements.txt
```

---

## 策略参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `MIN_GAP_PCT` | 0.10 | 触发信号的最小价格差距（%） |
| `ENTRY_MARGIN` | 0.03 | 理论胜率 - 市场赔率的最小安全边际 |
| `MIN_EV_THRESHOLD` | 0.02 | 最低期望收益（每单位） |
| `MAX_BET_FRACTION` | 0.05 | 单笔最大仓位（总资金比例） |
| `MAX_DAILY_LOSS_FRACTION` | 0.15 | 日最大亏损比例（触发停机） |

---

## 回测结果（2026-03-10 ~ 2026-03-17）

| 指标 | 数值 |
|------|------|
| 第4分钟 gap ≥ 0.10% 时真实胜率 | **96.8%** |
| 动量策略胜率 | 46.1%（不能用）|
| 市场折扣假设 7% 时的模拟收益 | +768% / 7天 |

---

## 风险提示

1. **本项目仅供学习研究**，不构成投资建议
2. 加密货币预测市场存在合规风险，请确认当地法规
3. 策略盈利高度依赖「市场定价低于真实胜率」这一条件，需通过 Phase 1 验证
4. 实盘前务必完成纸面交易验证（100次以上）
5. 私钥安全至关重要，使用专用小额钱包，与主要资产完全隔离
