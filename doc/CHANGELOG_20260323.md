# 策略优化更新日志（2026-03-23）

## 背景

基于实盘数据（137笔交易，胜率81%，盈亏比1:4.1）的深度分析，针对性优化路径2（赔率强信号）的准入门槛。

**核心问题**：
- 胜率81%低于回测94%（-13%）
- 盈亏比1:4.1（亏损单金额是盈利单的4倍）
- 80%亏损单发生在"低波动"环境
- 震荡市（可信度<0.55）信号不可靠

## 改动清单

### P0优化（已实施）

#### 1. 低波动环境路径2赔率门槛提高

**文件**: `src/strategy.py` - `_eval_odds_only()`

**改动**:
```python
# 新增参数
def _eval_odds_only(..., vol_level: str = "medium"):
    # 低波动环境提高赔率门槛至0.85
    if vol_level == "low" and odds_px < 0.85:
        logger.debug("低波动环境拦截: vol=%s odds=%.2f < 0.85", vol_level, odds_px)
        return None
```

**数据依据**: 实盘亏损单中80%发生在"波动↓"环境

**预期效果**: 过滤掉#319类"gap≈0 + 赔率0.78 + 低波动"的弱信号

---

#### 2. 震荡市双确认加强

**文件**: `src/strategy.py` - `_eval_odds_only()`

**改动**:
```python
# 震荡市（conf<0.55）要求：赔率≥0.85 或 gap同向≥0.10%
if minute <= 3 and signal_confidence < 0.55:
    if odds_px < 0.85 and not (gap_same_dir and abs(gap) >= 0.10):
        logger.debug("震荡市拦截: conf=%.2f odds=%.2f gap=%.3f%%", ...)
        return None
```

**数据依据**: #326/#327在震荡市（conf=0.48）下单后反向

**预期效果**: 震荡市只接受"高赔率(≥0.85)"或"强gap(≥0.10%)"的双确认信号

---

#### 3. 分3高赔率gap门槛提高

**文件**: `src/strategy.py` - `_eval_odds_only()`

**改动**:
```python
# 分3高赔率gap门槛从0.05提高到0.10
if minute >= 3 and odds_px > 0.85:
    gap_ok  = gap_same_dir and abs(gap) >= 0.10  # 从0.05→0.10
    conf_ok = signal_confidence >= 0.60
```

**数据依据**: #319 Up@0.78 gap+0.02% -$4.95；#325 Down@0.785 gap-0.01% -$3.92

**预期效果**: 分3高赔率必须有"gap≥0.10% + conf≥0.60"双重保障

---

### P1优化（已实施）

#### 4. 路径2 EV门槛动态调整

**文件**: `src/strategy.py` - `_eval_odds_only()`

**改动**:
```python
# EV门槛根据市场环境动态调整
if vol_level == "low":
    ev_threshold = base_ev * 2.0  # 低波动：EV门槛翻倍
elif _local_hour == 23:
    ev_threshold = base_ev * 1.75  # 23点高波动时段
elif signal_confidence < 0.50:
    ev_threshold = base_ev * 1.5  # 低可信度
else:
    ev_threshold = base_ev
```

**数据依据**: 低波动+震荡市时，即使EV达标，信号质量仍不足

**预期效果**: 低波动环境下，只接受EV≥0.16（greed=5时base_ev=0.08×2）的超强信号

---

### 配套改动

#### 5. bot.py传递波动率等级

**文件**: `src/bot.py` - `_tick()`

**改动**:
```python
# 提取波动率等级（用于路径2优化）
vol_level = "medium"
if self.mc is not None:
    try:
        ctx = self.mc.get_context()
        if ctx:
            vol_level = ctx.get("vol_level", "medium")
    except Exception:
        pass

# 传递给策略
signal = self.strategy.evaluate(..., vol_level=vol_level)
```

---

## 预期效果

| 指标 | 当前值 | 目标值 | 说明 |
|------|--------|--------|------|
| 胜率 | 81.0% | ≥85% | 提升4%+ |
| 盈亏比 | 1:4.1 | ≤1:3 | 改善25%+ |
| 日均信号 | ~40笔 | 25~35笔 | 下降20~30% |
| 低波动亏损率 | >80% | <50% | 显著改善 |

## 风险提示

1. **过度拟合风险**: 优化基于137笔样本，需更多数据验证
2. **信号量下降**: 预计日均信号从40笔降至25~30笔
3. **错失机会**: 部分"低波动+中赔率"的真实机会会被过滤

## 监控计划

**观察期**: 3~5天

**监控指标**:
- 胜率变化（目标：81%→85%+）
- 信号量变化（预期：-20%~-30%）
- 盈亏比（目标：1:4.1→1:3以内）
- 低波动环境亏损率（目标：<50%）

**后续决策**:
- 若胜率提升至85%+且盈亏比改善，保持当前配置
- 若信号量过少（<20笔/天），考虑微调门槛
- 若效果不明显，考虑调整贪婪指数至4（更保守）

## 回滚方案

如需回滚，执行：
```bash
git checkout HEAD~1 src/strategy.py src/bot.py
```

或手动移除以下改动：
1. `_eval_odds_only()` 的 `vol_level` 参数
2. 低波动环境赔率门槛检查
3. 震荡市双确认检查
4. 分3高赔率gap门槛从0.10改回0.05
5. EV门槛动态调整逻辑

---

**总结**: 核心思路是"在不确定环境下提高准入门槛"，用信号量换胜率和盈亏比。
