#!/bin/bash
# 策略优化部署脚本
# 用途：自动停止旧bot，部署新策略，启动新bot

set -e  # 遇到错误立即退出

echo "=========================================="
echo "策略优化部署脚本"
echo "=========================================="
echo ""

# 1. 检查当前目录
if [ ! -f "src/bot.py" ]; then
    echo "❌ 错误: 请在项目根目录运行此脚本"
    exit 1
fi

echo "✅ 当前目录: $(pwd)"
echo ""

# 2. 停止旧bot
echo "🛑 停止旧bot进程..."
pkill -f "python.*bot.py" || echo "  (没有运行中的bot)"
sleep 2
echo ""

# 3. 检查git状态
echo "📦 检查代码状态..."
git status --short
if [ -n "$(git status --porcelain)" ]; then
    echo "⚠️  警告: 工作区有未提交的改动"
    read -p "是否继续部署? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "❌ 部署已取消"
        exit 1
    fi
fi
echo ""

# 4. 创建日志目录
echo "📁 准备日志目录..."
mkdir -p logs
echo "  logs/ 目录已就绪"
echo ""

# 5. 备份旧日志（如果存在）
TODAY=$(date +%Y%m%d)
if [ -f "logs/bot_$TODAY.log" ]; then
    echo "💾 备份今日旧日志..."
    mv "logs/bot_$TODAY.log" "logs/bot_${TODAY}_backup_$(date +%H%M%S).log"
    echo "  已备份为: logs/bot_${TODAY}_backup_$(date +%H%M%S).log"
    echo ""
fi

# 6. 启动新bot
echo "🚀 启动优化后的bot..."
nohup python3 -m src.bot --mode live > "logs/bot_$TODAY.log" 2>&1 &
BOT_PID=$!
echo "  Bot PID: $BOT_PID"
echo ""

# 7. 等待启动
echo "⏳ 等待bot启动（5秒）..."
sleep 5
echo ""

# 8. 验证启动
echo "🔍 验证bot状态..."
if ps -p $BOT_PID > /dev/null; then
    echo "  ✅ Bot进程运行中 (PID: $BOT_PID)"
else
    echo "  ❌ Bot启动失败，请检查日志"
    tail -20 "logs/bot_$TODAY.log"
    exit 1
fi
echo ""

# 9. 显示启动日志
echo "📋 最近日志（最后20行）:"
echo "----------------------------------------"
tail -20 "logs/bot_$TODAY.log"
echo "----------------------------------------"
echo ""

# 10. 完成提示
echo "=========================================="
echo "✅ 部署完成！"
echo "=========================================="
echo ""
echo "📊 监控命令:"
echo "  实时日志:   tail -f logs/bot_$TODAY.log"
echo "  拦截统计:   grep -E '拦截|双确认' logs/bot_$TODAY.log | wc -l"
echo "  下单统计:   grep '✅ 下单成功' logs/bot_$TODAY.log | wc -l"
echo "  结算统计:   grep '结算.*✅\\|结算.*❌' logs/bot_$TODAY.log"
echo ""
echo "⚠️  重要提示:"
echo "  • 观察期: 3~5天（至2026-03-28）"
echo "  • 目标胜率: ≥85% (当前81%)"
echo "  • 预期信号量: 25~35笔/天 (当前~40笔)"
echo "  • 复盘时间: 2026-03-28"
echo ""
echo "📖 详细文档:"
echo "  优化方案: doc/STRATEGY_OPTIMIZATION_20260323.md"
echo "  部署指南: doc/DEPLOYMENT_GUIDE_20260323.md"
echo ""
