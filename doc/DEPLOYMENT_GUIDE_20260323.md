# 策略优化部署指南

## 当前状态

✅ **代码优化已完成并提交**
- Commit: `eb322c7` - feat: 策略优化 - 低波动/震荡市环境准入门槛提高
- 改动: 8个文件，+739/-35行
- 文档: 5份完整的分析和优化文档

## 立即部署步骤

### 1. 停止当前运行的bot（如果有）

```bash
# 查找运行中的bot进程
ps aux | grep "python.*bot.py"

# 停止进程（替换<PID>为实际进程ID）
kill <PID>

# 或使用pkill
pkill -f "python.*bot.py"
```

### 2. 拉取最新代码（如果在远程服务器）

```bash
git pull origin main
```

### 3. 启动优化后的bot

```bash
# 确保在项目根目录
cd /Users/tianwanggaidihu/src/fuck_polymarket

# 启动实盘模式（后台运行）
nohup python -m src.bot --mode live > logs/bot_$(date +%Y%m%d).log 2>&1 &

# 查看启动日志
tail -f logs/bot_$(date +%Y%m%d).log
```

### 4. 验证启动成功

检查日志中是否出现：
```
INFO     bot   模式: LIVE  资金: $XX.XX (链上余额)
INFO     bot   [HH:MM:SS] 分X | BTC=$XXXXX ...
```

## 监控要点（前3天）

### 每日检查清单

#### 1. 胜率监控
```bash
# 从日志提取结算数据
grep "结算.*✅\|结算.*❌" logs/bot_$(date +%Y%m%d).log | tail -20
```

**目标**: 胜率从81%提升至85%+

#### 2. 信号量监控
```bash
# 统计当日下单数
grep "✅ 下单成功" logs/bot_$(date +%Y%m%d).log | wc -l
```

**预期**: 25~35笔/天（比优化前减少20~30%）
**警戒**: 如果<20笔/天，需要微调门槛

#### 3. 拦截日志监控
```bash
# 查看优化拦截生效情况
grep -E "低波动环境拦截|震荡市拦截|分3高赔率双确认拦截" logs/bot_$(date +%Y%m%d).log | wc -l
```

**预期**: 每天应该有10~20次拦截（说明优化在工作）

#### 4. 盈亏比监控
```bash
# 查看最近结算
grep "结算.*PnL" logs/bot_$(date +%Y%m%d).log | tail -30
```

**目标**: 盈亏比从1:4.1改善至1:3以内

### 关键指标对比表

| 指标 | 优化前 | 目标 | 实际 | 达成 |
|------|--------|------|------|------|
| 胜率 | 81.0% | ≥85% | _待填_ | ⬜ |
| 盈亏比 | 1:4.1 | ≤1:3 | _待填_ | ⬜ |
| 日均信号 | ~40笔 | 25~35笔 | _待填_ | ⬜ |
| 低波动亏损率 | >80% | <50% | _待填_ | ⬜ |

## 3天后复盘

### 数据收集

```bash
# 提取3天的结算数据
for date in $(seq -f "%Y%m%d" $(date -v-2d +%Y%m%d) $(date +%Y%m%d)); do
    echo "=== $date ==="
    grep "结算.*✅\|结算.*❌" logs/bot_$date.log 2>/dev/null | wc -l
done
```

### 决策树

```
效果评估
├─ 胜率≥85% 且 盈亏比≤1:3
│  └─ ✅ 保持当前配置，继续观察
│
├─ 胜率提升但信号量<20笔/天
│  └─ ⚠️ 微调门槛：
│     - 震荡市conf门槛从0.55降至0.52
│     - 或分3高赔率gap从0.10降至0.08
│
├─ 胜率提升不明显（<83%）
│  └─ 🔧 考虑更激进优化：
│     - 降低贪婪指数至4（更保守）
│     - 或提高所有路径的最低赔率至0.80
│
└─ 胜率下降或盈亏比恶化
   └─ ⚠️ 立即回滚：
      git checkout HEAD~1 src/strategy.py src/bot.py scripts/market_context.py
```

## 常见问题

### Q1: 如何快速回滚？

```bash
# 停止bot
pkill -f "python.*bot.py"

# 回滚代码
git checkout HEAD~1 src/strategy.py src/bot.py scripts/market_context.py

# 重启bot
nohup python -m src.bot --mode live > logs/bot_$(date +%Y%m%d).log 2>&1 &
```

### Q2: 如何调整贪婪指数？

编辑 `.env` 文件：
```bash
# 当前: GREED_INDEX=5（平衡）
# 更保守: GREED_INDEX=4
# 更激进: GREED_INDEX=6
```

重启bot生效。

### Q3: 如何查看实时拦截情况？

```bash
# 实时监控拦截日志
tail -f logs/bot_$(date +%Y%m%d).log | grep -E "拦截|双确认"
```

## 紧急联系

如遇到以下情况，立即停止bot并检查：
- ❌ 连续5笔亏损
- ❌ 单日亏损超过15%
- ❌ 出现异常大额下单（>$10）
- ❌ 日志出现大量ERROR

停止命令：
```bash
pkill -f "python.*bot.py"
```

---

**部署时间**: 待执行
**预计观察期**: 2026-03-23 ~ 2026-03-28（5天）
**复盘时间**: 2026-03-28

**优化目标**: 用信号量换胜率和盈亏比，提升策略稳定性。
