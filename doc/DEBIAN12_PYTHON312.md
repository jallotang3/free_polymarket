# Debian 12 上安装 Python 3.12（用于本仓库依赖）

**一键脚本（通用、可复现）**：仓库内 [debian/setup_env.sh](../debian/setup_env.sh)，说明见 [debian/README.md](../debian/README.md)。脚本通过 **[uv](https://github.com/astral-sh/uv)** 安装 **预编译** Python 3.12，无需本机编译，通常数分钟内完成。

---

## 更快的方式（不编译源码）

**debian/setup_env.sh** 已默认用 **uv** 装预编译 Python（见文档开头）。若你**不用脚本**、想手工安装，可选用下面之一（均为下载预编译解释器，通常几分钟内可用）。

### A. uv（与脚本相同：单二进制 + 预编译 CPython）

[Astral uv](https://github.com/astral-sh/uv) 在 Linux 上会拉取 **独立构建的 CPython**（无需本机 `make`）。

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# 按提示把 ~/.local/bin 加入 PATH 后，新开终端或 source 对应 rc

cd /path/to/fuck_polymarket
uv python install 3.12
uv venv --python 3.12 .venv
uv pip install -r requirements.txt
```

### B. Docker（宿主机完全不装 3.12）

本仓库在 [docker/](../docker/) 提供 `Dockerfile` 与 `docker-compose.yml`，说明见 [docker/README.md](../docker/README.md)。镜像基于 `python:3.12-bookworm`，`data/`、`logs/` 与 `.env` 通过挂载 / `env_file` 使用，适合已装 Docker 的服务器。

### C. micromamba / Miniforge（conda-forge 二进制）

```bash
curl -Ls https://micro.mamba.pm/install.sh | bash
# 按提示初始化 shell 后
micromamba create -n pm -c conda-forge python=3.12 -y
micromamba activate pm
pip install -r requirements.txt
```

### 对比 pyenv 源码编译

| 方式 | 大致耗时 | 说明 |
|------|----------|------|
| uv / micromamba | 通常数分钟内 | 下载解压为主 |
| Docker 拉镜像 | 视网络，一次性 | 之后复用快 |
| pyenv `install` | 常 10–30+ 分钟 | 本机编译，最慢但无第三方运行时依赖 |

---

## 为什么会出现 `Unable to locate package python3.12`

- **Debian 12（bookworm）稳定源**里默认只有 **Python 3.11**，**没有**名为 `python3.12` 的官方套件。
- 依赖 `polymarket-apis` 的近期版本声明 **Requires-Python ≥ 3.12**，因此必须在机器上使用 **3.12+** 的解释器建虚拟环境，而不是系统自带的 `python3`（3.11）。

下面给出三种常用做法，任选其一即可。

---

## 方式一：pyenv（推荐，不污染系统 Python）

```bash
# 编译依赖（Debian 12）
sudo apt update
sudo apt install -y make build-essential libssl-dev zlib1g-dev \
  libbz2-dev libreadline-dev libsqlite3-dev curl \
  libncursesw5-dev xz-utils tk-dev libffi-dev liblzma-dev git

# 安装 pyenv（官方脚本）
curl https://pyenv.run | bash
# 按提示把 pyenv 初始化写入 ~/.bashrc，然后重新登录或 source ~/.bashrc

pyenv install 3.12.8
cd /path/to/fuck_polymarket
pyenv local 3.12.8
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

---

## 方式二：从 python.org 源码编译安装

```bash
sudo apt update
sudo apt install -y build-essential zlib1g-dev libssl-dev libffi-dev \
  libbz2-dev libreadline-dev libsqlite3-dev curl

cd /tmp
curl -O https://www.python.org/ftp/python/3.12.8/Python-3.12.8.tgz
tar xf Python-3.12.8.tgz && cd Python-3.12.8
./configure --prefix=$HOME/.local/python-3.12 --enable-optimizations
make -j$(nproc) && make install

# 使用
~/\.local/python-3.12/bin/python3.12 -m venv /path/to/fuck_polymarket/.venv
```

---

## 方式三：Docker（详情见上文「更快的方式 → B」）

与「更快的方式」里 Docker 一段相同：宿主机不装 Python 3.12，用 `python:3.12-bookworm` 等镜像挂载项目与 `.env` 运行。

---

## 不推荐的做法

- **从 testing/sid 混装 `python3.12`**：容易拉进大量依赖版本，升级时风险高，除非你熟悉 apt pinning。
- **强行降低 `polymarket-apis` 版本**：旧版可能同样要求 3.12，或与当前代码不兼容；自动兑换功能也可能异常。

---

## 验证

```bash
python3.12 --version   # 或 pyenv 下的 python --version
# 应显示 3.12.x

.venv/bin/python -m pip install -r requirements.txt
# 应能成功安装 polymarket-apis
```
