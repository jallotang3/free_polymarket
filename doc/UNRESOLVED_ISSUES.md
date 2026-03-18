# 未解决问题清单

本文档记录当前系统中已识别但尚未解决的问题，供后续迭代参考。

---

## Issue #1：无法获取真实 PtB（Price-to-Beat）

### 问题描述

Polymarket BTC 5分钟市场的结算价（PtB）是窗口开始时刻 **Chainlink Data Streams** 的 BTC/USD 价格。
该价格由 Polymarket 内部通过付费企业级 Chainlink 服务获取，**不通过任何公开 API 暴露**。

### 影响

- 我们的 gap 计算（当前价格 vs PtB）存在误差，因为使用的是链上标准 Aggregator 的近似值
- 链上聚合器（Polygon PoS，`0x...`）更新频率约 15-60 秒一次，与 Data Streams 的毫秒级推送存在时差
- 在价格剧烈波动时，PtB 误差可能导致 gap 方向判断错误

### 已调查的途径

| 途径 | 结论 |
|------|------|
| Gamma API (`gamma-api.polymarket.com`) | 仅返回 UP/DOWN token 赔率，无 PtB 数值 |
| CLOB API (`clob.polymarket.com`) | 提供实时赔率、历史赔率，无 PtB 数值 |
| py-clob-client 官方 SDK | 封装 CLOB API，无 PtB 相关方法 |
| Chainlink 公开 Feeds API | 返回 404，无公开 HTTP 接口 |
| Chainlink 链上聚合器（Polygon RPC） | ✅ 可用，但是 Standard Aggregator，非 Data Streams，存在延迟差 |

### 当前应对方案

1. **主要信号来源改为 CLOB 实时赔率**（市场做市商拥有 Data Streams 访问权限，赔率隐含了真实 PtB 方向）
2. Chainlink 链上价格作为 gap 计算的最优近似（优先级高于 Binance/CryptoCompare）
3. 赔率强跳（`delta >= 0.15`）作为独立信号，绕过 gap 的方向依赖

### 待探索方案

- [ ] 分析 Polymarket 智能合约事件日志，查看结算时是否有链上 PtB 记录
- [ ] 通过 The Graph 协议查询 Polymarket 合约的历史解析数据，反推 PtB
- [ ] 联系 Polymarket 官方，询问是否有 PtB 数据的 API 访问方案（可能需要合作协议）

---

## Issue #2：赔率强跳信号的独立路径尚未完整实现

### 问题描述

在 [过去的对话分析](f1dff702-6e66-4bb2-b3c6-073ab5738f83) 中识别到：当 CLOB 赔率出现强跳（如 Up=0.80 在分1出现）时，
现有逻辑要求 `abs(gap) >= 0.05%` 且 `minute >= 3` 才触发，导致高置信度信号被错误过滤。

### 影响示例

日志片段（03:45 窗口）：
- 分1 Up=0.80 → 赔率极强，但 gap 仅 +0.023%（低于阈值）→ `⚪ 无信号`
- 分4 Down=0.70 赔率大幅反转 → 实际方向为 UP

### 待实现

- [ ] 在 `analyze_opportunity()` 中增加"赔率强确认独立信号"路径：当赔率单边强度超过阈值（如 `>= 0.72`）且发生过跳变时，不强制要求 gap 方向一致，直接触发信号

---

*最后更新：2026-03-17*
