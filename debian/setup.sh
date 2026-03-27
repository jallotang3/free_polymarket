#!/usr/bin/env bash
# debian/setup.sh — Debian 12 一键部署脚本
# 功能：安装 Docker、构建镜像、注册 systemd 服务（开机自启，实盘模式）
#
# 用法（在仓库根目录执行）：
#   sudo bash debian/setup.sh
#
# 前置：
#   1. 仓库已 clone 到目标机器
#   2. 根目录存在 .env（可从 .env.example 复制后填写）

set -euo pipefail

# ── 颜色输出 ──────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()   { echo -e "${RED}[ERR]${NC}   $*" >&2; exit 1; }

# ── 必须以 root 运行 ──────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "请用 sudo 运行：sudo bash debian/setup.sh"

# ── 确认在仓库根目录 ──────────────────────────────────────────
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
[[ -f "$REPO_DIR/docker/docker-compose.yml" ]] \
    || die "未找到 docker/docker-compose.yml，请在仓库根目录执行"
[[ -f "$REPO_DIR/.env" ]] \
    || die ".env 不存在，请先复制并填写：cp .env.example .env"

SERVICE_NAME="polymarket-bot"
COMPOSE_FILE="$REPO_DIR/docker/docker-compose.yml"
OVERRIDE_FILE="$REPO_DIR/docker/docker-compose.override.yml"

# ── 1. 安装 Docker ────────────────────────────────────────────
if command -v docker &>/dev/null; then
    info "Docker 已安装：$(docker --version)"
else
    info "正在安装 Docker..."
    apt-get update -qq
    apt-get install -y -qq curl ca-certificates
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
    sh /tmp/get-docker.sh
    rm -f /tmp/get-docker.sh
    systemctl enable --now docker
    info "Docker 安装完成：$(docker --version)"
fi

# ── 2. 确认 docker compose v2 可用 ───────────────────────────
docker compose version &>/dev/null \
    || die "docker compose v2 不可用，请升级 Docker（>= 23）"

# ── 3. 创建持久化目录 ─────────────────────────────────────────
mkdir -p "$REPO_DIR/data" "$REPO_DIR/logs"
info "持久化目录：$REPO_DIR/data  $REPO_DIR/logs"

# ── 4. 生成实盘 override（不修改原 docker-compose.yml）────────
if [[ ! -f "$OVERRIDE_FILE" ]]; then
    info "生成实盘 override：$OVERRIDE_FILE"
    cat > "$OVERRIDE_FILE" <<'YAML'
# 实盘覆盖（由 debian/setup.sh 生成）
# 删除此文件后重新运行 setup.sh 可重置为纸面模式
services:
  bot:
    command: ["python", "-m", "src.bot", "--mode", "live","--yes"]
YAML
else
    warn "override 已存在，跳过生成：$OVERRIDE_FILE"
fi

# ── 5. 构建镜像 ───────────────────────────────────────────────
info "构建 Docker 镜像（首次较慢）..."
docker compose -f "$COMPOSE_FILE" -f "$OVERRIDE_FILE" build --pull

# ── 6. 写入 systemd 服务单元 ──────────────────────────────────
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
info "写入 systemd 服务：$SERVICE_FILE"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Polymarket Trading Bot (live)
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
WorkingDirectory=$REPO_DIR
ExecStart=/usr/bin/docker compose -f $COMPOSE_FILE -f $OVERRIDE_FILE up --build
ExecStop=/usr/bin/docker compose  -f $COMPOSE_FILE -f $OVERRIDE_FILE down
Restart=on-failure
RestartSec=15s
StandardOutput=journal
StandardError=journal
SyslogIdentifier=$SERVICE_NAME

[Install]
WantedBy=multi-user.target
EOF

# ── 7. 启用并启动服务 ─────────────────────────────────────────
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

# ── 8. 完成提示 ───────────────────────────────────────────────
echo ""
info "✅ 部署完成！"
echo ""
echo "  实时日志：  journalctl -u $SERVICE_NAME -f"
echo "  容器日志：  docker compose -f $COMPOSE_FILE -f $OVERRIDE_FILE logs -f bot"
echo "  停止服务：  sudo systemctl stop $SERVICE_NAME"
echo "  禁用自启：  sudo systemctl disable $SERVICE_NAME"
echo ""
