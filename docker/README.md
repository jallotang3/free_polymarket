# Docker 运行

宿主机无需安装 Python 3.12，镜像基于 `python:3.12-bookworm`。

## 前置

1. 安装 [Docker](https://docs.docker.com/get-docker/) 与 Docker Compose v2。
2. 在**仓库根目录**准备 `.env`（可复制 `.env.example`）。Compose 通过 `env_file` 挂载，**不会**把 `.env` 打进镜像。

## 构建并启动（默认纸面）

在**仓库根目录**执行：

```bash
docker compose -f docker/docker-compose.yml up --build
```

或在 `docker/` 目录下：

```bash
docker compose up --build
```

`data/`、`logs/` 挂载到宿主机当前仓库下同名目录，数据库与日志持久化。

## 覆盖命令（实盘等）

```bash
docker compose -f docker/docker-compose.yml run --rm bot python -m src.bot --mode live --yes
```

后台常驻：

```bash
docker compose -f docker/docker-compose.yml up -d --build
docker compose -f docker/docker-compose.yml logs -f bot
```

## 仅构建镜像

```bash
docker build -f docker/Dockerfile -t fuck-polymarket-bot .
```

## 说明

- 若不存在 `.env`，Compose 可能报错；可先 `touch .env` 或从 `.env.example` 复制。
- 若 `.env` 里 `PROXY_URL` 为 `http://127.0.0.1:...`，容器内 `127.0.0.1` 不是宿主机。可改为 `http://host.docker.internal:端口`（Docker Desktop / 较新 Engine），或在 `docker-compose.yml` 中为 `bot` 增加 `extra_hosts: ["host.docker.internal:host-gateway"]`（Linux），或改用宿主机局域网 IP。
- 策略与配置说明仍以仓库根目录 [README.md](../README.md) 为准。
