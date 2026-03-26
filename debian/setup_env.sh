#!/usr/bin/env bash
# Debian（推荐 bookworm 12）一键安装：编译依赖 + pyenv + Python 3.12 + 项目 .venv
# 原因：稳定源无 python3.12 套件，polymarket-apis 需要 Python >= 3.12
#
# 用法：
#   chmod +x debian/setup_env.sh
#   ./debian/setup_env.sh
#
# 环境变量（可选）：
#   PYTHON_VERSION=3.12.8   默认 3.12.8
#   SKIP_APT=1              跳过 apt（已装过编译依赖时）
#   SKIP_PYENV=1            假定 pyenv 与 Python 已就绪，只做 venv + pip

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PY_VERSION="${PYTHON_VERSION:-3.12.8}"
VENV_DIR="${VENV_DIR:-${PROJECT_ROOT}/.venv}"

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  echo "选项:"
  echo "  -h, --help        显示说明"
  echo "  --system-only     只执行 apt 安装编译依赖（不装 pyenv / 不建 venv）"
  echo "  --skip-venv       只装系统依赖 + pyenv + Python，不创建 .venv、不 pip install"
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

# 常用工具：curl / wget / git（下载脚本、克隆依赖、pyenv 插件等）
APT_PACKAGES=(
  make build-essential ca-certificates
  curl wget git
  libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev
  libncursesw5-dev xz-utils tk-dev libffi-dev liblzma-dev
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
  log "安装 apt 编译依赖（Python ${PY_VERSION} 源码编译需要）…"
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    apt-get update
    apt-get install -y "${APT_PACKAGES[@]}"
  else
    sudo apt-get update
    sudo apt-get install -y "${APT_PACKAGES[@]}"
  fi
}

ensure_pyenv() {
  if [[ "${SKIP_PYENV:-0}" == "1" ]]; then
    log "SKIP_PYENV=1，跳过 pyenv 安装"
    export PATH="${HOME}/.pyenv/bin:${PATH}"
    return 0
  fi
  export PYENV_ROOT="${PYENV_ROOT:-${HOME}/.pyenv}"
  if [[ ! -d "${PYENV_ROOT}/bin" ]]; then
    log "安装 pyenv（官方脚本，目录: ${PYENV_ROOT}）…"
    curl -fsSL https://pyenv.run | bash
  fi
  export PATH="${PYENV_ROOT}/bin:${PATH}"
  # shellcheck disable=SC1090
  eval "$(pyenv init - bash)"

  # 持久化到 ~/.bashrc（幂等）
  if ! grep -qF 'pyenv init' "${HOME}/.bashrc" 2>/dev/null; then
    {
      echo ''
      echo '# pyenv (fuck_polymarket debian/setup_env.sh)'
      echo "export PYENV_ROOT=\"\${PYENV_ROOT:-\$HOME/.pyenv}\""
      echo 'export PATH="$PYENV_ROOT/bin:$PATH"'
      echo 'eval "$(pyenv init - bash)"'
    } >> "${HOME}/.bashrc"
    log "已追加 pyenv 初始化到 ~/.bashrc，新开终端或: source ~/.bashrc"
  fi
}

pyenv_install_python() {
  export PATH="${PYENV_ROOT:-${HOME}/.pyenv}/bin:${PATH}"
  # shellcheck disable=SC1090
  eval "$(pyenv init - bash)"

  if pyenv versions --bare 2>/dev/null | grep -qxF "${PY_VERSION}"; then
    log "Python ${PY_VERSION} 已安装（pyenv）"
  else
    log "编译并安装 Python ${PY_VERSION}（首次可能需数分钟）…"
    pyenv install "${PY_VERSION}"
  fi

  cd "${PROJECT_ROOT}"
  pyenv local "${PY_VERSION}"
  log "已在 ${PROJECT_ROOT} 设置 pyenv local ${PY_VERSION}"
}

create_venv_and_pip() {
  export PATH="${PYENV_ROOT:-${HOME}/.pyenv}/bin:${PATH}"
  # shellcheck disable=SC1090
  eval "$(pyenv init - bash)"
  cd "${PROJECT_ROOT}"

  pyenv local "${PY_VERSION}"
  local py="$(pyenv which python)"
  log "使用解释器: ${py}"

  if [[ -d "${VENV_DIR}" ]]; then
    log "已存在 ${VENV_DIR}，跳过创建（如需重建请手动删除该目录）"
  else
    log "创建虚拟环境: ${VENV_DIR}"
    "${py}" -m venv "${VENV_DIR}"
  fi

  log "升级 pip 并安装 requirements.txt"
  "${VENV_DIR}/bin/pip" install -U pip setuptools wheel
  "${VENV_DIR}/bin/pip" install -r "${PROJECT_ROOT}/requirements.txt"

  log "完成。"
  echo ""
  echo "  激活:  source ${VENV_DIR}/bin/activate"
  echo "  或:    ${PROJECT_ROOT}/run_bot.sh --mode paper --capital 1000"
}

# --- main ---
install_apt

if [[ "${SYSTEM_ONLY}" == "1" ]]; then
  log "仅系统依赖已安装（--system-only）。请自行安装 pyenv 与 Python ${PY_VERSION}。"
  exit 0
fi

ensure_pyenv
pyenv_install_python

if [[ "${SKIP_VENV}" == "1" ]]; then
  log "已跳过 venv（--skip-venv）。在项目目录执行: python -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 0
fi

create_venv_and_pip
