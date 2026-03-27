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
    echo "   polymarket-apis 需要 Python ≥3.12，请用 3.12 创建 venv，例如:"
    echo "   python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    echo "   无系统 3.12 时见: doc/DEBIAN12_PYTHON312.md 或 docker/README.md"
    exit 1
fi

if ! "$VENV_PYTHON" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 12) else 1)'; then
    ver="$("$VENV_PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    echo "❌ 需要 Python ≥3.12，当前 .venv 为 Python ${ver}"
    echo "   请删除 .venv 后用 3.12 重建: rm -rf .venv && python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    echo "   说明: doc/DEBIAN12_PYTHON312.md"
    exit 1
fi

echo "✅ 使用虚拟环境: $VENV_PYTHON"
exec "$VENV_PYTHON" -m src.bot "$@"
