#!/usr/bin/env bash
# Debian（推荐 bookworm 12）一键安装：uv + Python 3.12 + 项目 .venv
# 原因：稳定源无 python3.12 套件；uv 使用预编译 Python，比 pyenv 源码编译快得多
#
# 用法：
#   chmod +x debian/setup_env.sh
#   ./debian/setup_env.sh
#
# 环境变量（可选）：
#   PYTHON_VERSION=3.12.8   默认 3.12.8（uv python install）
#   SKIP_APT=1              跳过 apt（已装过基础工具时）
#   SKIP_UV=1               跳过安装 uv 可执行文件（需 PATH 中已有 uv）
#   SKIP_PYENV=1            同 SKIP_UV（兼容旧名）
#
# 请用 bash 或 ./ 运行；若误用 sh（常为 dash），下面会重入 bash。

if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PY_VERSION="${PYTHON_VERSION:-3.12.8}"
VENV_DIR="${VENV_DIR:-${PROJECT_ROOT}/.venv}"

usage() {
  sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
  echo "选项:"
  echo "  -h, --help        显示说明"
  echo "  --system-only     只执行 apt 安装基础包（不装 uv / 不建 venv）"
  echo "  --skip-venv       只装 apt + uv + Python，不创建 .venv、不 pip install"
}

SYSTEM_ONLY=0
SKIP_VENV=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --system-only) SYSTEM_ONLY=1; shift ;;
    --skip-venv) SKIP_VENV=1; shift ;;
    *) echo "未知参数: $1" >&2; usage >&2; exit 1 ;;
  esac
done

log() { echo "[setup_env] $*"; }

# curl：拉 uv 安装脚本；wget/git：日常与部分工具链
APT_PACKAGES=(
  ca-certificates
  curl wget git
  build-essential
)

install_apt() {
  if [[ "${SKIP_APT:-0}" == "1" ]]; then
    log "SKIP_APT=1，跳过 apt"
    return 0
  fi
  if [[ "${EUID:-$(id -u)}" -ne 0 ]] && ! command -v sudo >/dev/null 2>&1; then
    log "需要 root 或 sudo 以安装 apt 软件包"
    exit 1
  fi
  log "安装 apt 基础依赖（网络工具 + build-essential，供 uv 与偶发 pip 源码构建）…"
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    apt-get update
    apt-get install -y "${APT_PACKAGES[@]}"
  else
    sudo apt-get update
    sudo apt-get install -y "${APT_PACKAGES[@]}"
  fi
}

prepend_path_local_bin() {
  export PATH="${HOME}/.local/bin:${PATH}"
}

ensure_uv() {
  prepend_path_local_bin
  if [[ "${SKIP_UV:-0}" == "1" ]] || [[ "${SKIP_PYENV:-0}" == "1" ]]; then
    log "SKIP_UV=1（或 SKIP_PYENV=1），跳过 uv 安装脚本；请确保 uv 已在 PATH"
    if ! command -v uv >/dev/null 2>&1; then
      log "未找到 uv，请先安装: curl -LsSf https://astral.sh/uv/install.sh | sh"
      exit 1
    fi
    return 0
  fi
  if command -v uv >/dev/null 2>&1; then
    log "已存在 uv: $(command -v uv)"
  else
    log "安装 uv（官方脚本，目录: ~/.local/bin）…"
    curl -LsSf https://astral.sh/uv/install.sh | sh
  fi
  prepend_path_local_bin
  if ! command -v uv >/dev/null 2>&1; then
    log "无法找到 uv，请执行: export PATH=\"\$HOME/.local/bin:\$PATH\""
    exit 1
  fi

  if ! grep -qF '.local/bin' "${HOME}/.bashrc" 2>/dev/null; then
    {
      echo ''
      echo '# uv PATH (fuck_polymarket debian/setup_env.sh)'
      echo 'export PATH="$HOME/.local/bin:$PATH"'
    } >> "${HOME}/.bashrc"
    log "已追加 ~/.local/bin 到 PATH（~/.bashrc），新开终端或: source ~/.bashrc"
  fi
}

uv_install_python() {
  prepend_path_local_bin
  if ! command -v uv >/dev/null 2>&1; then
    log "缺少 uv"
    exit 1
  fi
  log "安装 Python ${PY_VERSION}（uv 预编译发行版，通常数分钟内完成）…"
  uv python install "${PY_VERSION}"
  log "Python ${PY_VERSION} 已就绪（由 uv 管理）"
}

create_venv_and_pip() {
  prepend_path_local_bin
  cd "${PROJECT_ROOT}"

  if [[ -d "${VENV_DIR}" ]]; then
    log "已存在 ${VENV_DIR}，跳过创建（如需重建请手动删除该目录）"
  else
    log "创建虚拟环境: ${VENV_DIR}"
    uv venv --python "${PY_VERSION}" "${VENV_DIR}"
  fi

  log "安装 requirements.txt（uv pip）"
  uv pip install -r "${PROJECT_ROOT}/requirements.txt" --python "${VENV_DIR}/bin/python"

  log "完成。"
  echo ""
  echo "  激活:  source ${VENV_DIR}/bin/activate"
  echo "  或:    ${PROJECT_ROOT}/run_bot.sh --mode paper --capital 1000"
}

# --- main ---
install_apt

if [[ "${SYSTEM_ONLY}" == "1" ]]; then
  log "仅系统依赖已安装（--system-only）。请自行安装 uv 并: uv python install ${PY_VERSION}"
  exit 0
fi

ensure_uv
uv_install_python

if [[ "${SKIP_VENV}" == "1" ]]; then
  log "已跳过 venv（--skip-venv）。在项目目录执行:"
  log "  uv venv --python ${PY_VERSION} .venv && uv pip install -r requirements.txt --python .venv/bin/python"
  exit 0
fi

create_venv_and_pip
