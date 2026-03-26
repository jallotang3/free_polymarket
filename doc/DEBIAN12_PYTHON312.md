# Debian 12 上安装 Python 3.12（用于本仓库依赖）

**一键脚本（推荐）**：仓库内 [debian/setup_env.sh](../debian/setup_env.sh)，说明见 [debian/README.md](../debian/README.md)。

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

## 方式三：Docker（不在宿主机装 3.12）

使用官方镜像 `python:3.12-bookworm`，在容器内挂载项目目录与 `.env` 运行；适合服务器上已有 Docker 的场景。

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
