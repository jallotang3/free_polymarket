#!/bin/bash
# 策略优化监控脚本
# 用途：每日检查优化效果

set -e

TODAY=$(date +%Y%m%d)
LOG_FILE="logs/bot_$TODAY.log"

if [ ! -f "$LOG_FILE" ]; then
    echo "❌ 错误: 日志文件不存在: $LOG_FILE"
    exit 1
fi

echo "=========================================="
echo "策略优化监控报告 - $(date +%Y-%m-%d)"
echo "=========================================="
echo ""

# 1. 下单统计
echo "📊 下单统计"
echo "----------------------------------------"
TOTAL_ORDERS=$(grep -c "✅ 下单成功" "$LOG_FILE" || echo "0")
echo "  总下单数: $TOTAL_ORDERS 笔"
echo ""

# 2. 结算统计
echo "📈 结算统计"
echo "----------------------------------------"
TOTAL_SETTLED=$(grep -cE "结算.*✅|结算.*❌" "$LOG_FILE" || echo "0")
WINS=$(grep -c "结算.*✅" "$LOG_FILE" || echo "0")
LOSSES=$(grep -c "结算.*❌" "$LOG_FILE" || echo "0")

if [ $TOTAL_SETTLED -gt 0 ]; then
    WIN_RATE=$(echo "scale=1; $WINS * 100 / $TOTAL_SETTLED" | bc)
    echo "  总结算数: $TOTAL_SETTLED 笔"
    echo "  盈利单:   $WINS 笔"
    echo "  亏损单:   $LOSSES 笔"
    echo "  胜率:     $WIN_RATE%"

    # 胜率评估
    if (( $(echo "$WIN_RATE >= 85" | bc -l) )); then
        echo "  状态:     ✅ 达标 (目标≥85%)"
    elif (( $(echo "$WIN_RATE >= 83" | bc -l) )); then
        echo "  状态:     ⚠️  接近目标"
    else
        echo "  状态:     ❌ 未达标"
    fi
else
    echo "  (今日暂无结算数据)"
fi
echo ""

# 3. 拦截统计
echo "🛡️  优化拦截统计"
echo "----------------------------------------"
LOW_VOL=$(grep -c "低波动环境拦截" "$LOG_FILE" || echo "0")
SHOCK=$(grep -c "震荡市拦截" "$LOG_FILE" || echo "0")
HIGH_ODDS=$(grep -c "分3高赔率双确认拦截" "$LOG_FILE" || echo "0")
TOTAL_BLOCKS=$((LOW_VOL + SHOCK + HIGH_ODDS))

echo "  低波动拦截:   $LOW_VOL 次"
echo "  震荡市拦截:   $SHOCK 次"
echo "  高赔率拦截:   $HIGH_ODDS 次"
echo "  总拦截次数:   $TOTAL_BLOCKS 次"

if [ $TOTAL_BLOCKS -gt 0 ]; then
    echo "  状态:         ✅ 优化生效中"
else
    echo "  状态:         ⚠️  拦截较少，可能市场环境良好"
fi
echo ""

# 4. 最近结算
echo "📋 最近10笔结算"
echo "----------------------------------------"
grep -E "结算.*✅|结算.*❌" "$LOG_FILE" | tail -10 | while read line; do
    if echo "$line" | grep -q "✅"; then
        echo "  ✅ $(echo $line | grep -oE 'id=[0-9]+' | cut -d= -f2) | $(echo $line | grep -oE 'PnL=[+-][0-9.]+' | cut -d= -f2)"
    else
        echo "  ❌ $(echo $line | grep -oE 'id=[0-9]+' | cut -d= -f2) | $(echo $line | grep -oE 'PnL=[+-][0-9.]+' | cut -d= -f2)"
    fi
done
echo ""

# 5. 风控状态
echo "⚠️  风控状态"
echo "----------------------------------------"
RISK_ALERTS=$(grep -c "风控拦截" "$LOG_FILE" || echo "0")
if [ $RISK_ALERTS -gt 0 ]; then
    echo "  ⚠️  今日触发风控 $RISK_ALERTS 次"
    grep "风控拦截" "$LOG_FILE" | tail -3
else
    echo "  ✅ 无风控告警"
fi
echo ""

# 6. 错误检查
echo "🔍 错误检查"
echo "----------------------------------------"
ERRORS=$(grep -c "ERROR" "$LOG_FILE" || echo "0")
if [ $ERRORS -gt 0 ]; then
    echo "  ⚠️  发现 $ERRORS 个错误"
    grep "ERROR" "$LOG_FILE" | tail -3
else
    echo "  ✅ 无错误"
fi
echo ""

# 7. 建议
echo "💡 建议"
echo "----------------------------------------"
if [ $TOTAL_SETTLED -lt 5 ]; then
    echo "  • 样本量较少，建议继续观察"
elif [ $TOTAL_SETTLED -ge 20 ]; then
    if (( $(echo "$WIN_RATE >= 85" | bc -l) )); then
        echo "  • ✅ 优化效果显著，保持当前配置"
    elif (( $(echo "$WIN_RATE < 83" | bc -l) )); then
        echo "  • ⚠️  胜率未达预期，考虑进一步优化"
    else
        echo "  • 📊 胜率接近目标，继续观察"
    fi

    if [ $TOTAL_ORDERS -lt 20 ]; then
        echo "  • ⚠️  信号量偏少，可能需要微调门槛"
    fi
fi
echo ""

echo "=========================================="
echo "监控完成 - $(date +%H:%M:%S)"
echo "=========================================="
echo ""
echo "📖 查看完整日志: tail -f $LOG_FILE"
echo "📊 详细分析: doc/DEPLOYMENT_GUIDE_20260323.md"
echo ""
