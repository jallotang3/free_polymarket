#!/usr/bin/env bash
# 使用项目虚拟环境运行 bot，确保所有依赖可用
# 用法：
#   ./run_bot.sh                    # paper 模式，资金 $1000
#   ./run_bot.sh --mode live        # 实盘
#   ./run_bot.sh --capital 500      # 指定资金
#   ./run_bot.sh --mode live --capital 100 --debug

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python3"

if [ ! -f "$VENV_PYTHON" ]; then
    echo "❌ 虚拟环境未找到: $VENV_PYTHON"
    echo "   请先运行: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

echo "✅ 使用虚拟环境: $VENV_PYTHON"
exec "$VENV_PYTHON" -m src.bot "$@"
