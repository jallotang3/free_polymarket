# Debian 环境安装脚本

## 脚本说明

| 文件 | 作用 |
|------|------|
| `setup_env.sh` | 在 Debian（推荐 12 bookworm）上安装 **uv**、通过 **uv** 安装 **Python 3.12.x**（预编译），在项目根目录创建 `.venv` 并用 **uv pip** 安装 `requirements.txt` |

稳定仓库没有 `python3.12` 软件包；`polymarket-apis` 需要 **Python ≥ 3.12**。脚本使用 [uv](https://github.com/astral-sh/uv) 下载官方维护的预编译 Python，**无需本机编译**，通常比旧版 pyenv 源码安装快得多。其他方式见 [doc/DEBIAN12_PYTHON312.md](../doc/DEBIAN12_PYTHON312.md)（Docker、手工 micromamba 等）。

`apt` 阶段会安装 **`curl`、`wget`、`git`**、`ca-certificates` 与 **`build-essential`**（拉取 uv、日常工具；少数 pip 包若需从源码构建时可用）。

## 用法

在项目根目录执行：

```bash
chmod +x debian/setup_env.sh
./debian/setup_env.sh
```

首次会下载 uv 与 Python 解释器，请保持网络畅通（通常数分钟级，视带宽而定）。

## 选项

```bash
./debian/setup_env.sh --help

# 只安装 apt 基础包（不装 uv / 不建 venv）
./debian/setup_env.sh --system-only

# 装 apt + uv + Python，但不创建 .venv、不 pip install
./debian/setup_env.sh --skip-venv
```

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `PYTHON_VERSION` | `3.12.8` | `uv python install` 的版本 |
| `SKIP_APT=1` | 关闭 | 已装过基础包时跳过 `apt-get` |
| `SKIP_UV=1` | 关闭 | 跳过 uv 安装脚本；需已安装 `uv` 且在 `PATH`（如 `~/.local/bin`） |
| `SKIP_PYENV=1` | 关闭 | 与 `SKIP_UV=1` 同义（兼容旧名） |

## 验证

```bash
source .venv/bin/activate
python --version   # Python 3.12.x
uv pip install -r requirements.txt --python .venv/bin/python   # 应能安装 polymarket-apis
```

更详细的背景与替代方案见 **[doc/DEBIAN12_PYTHON312.md](../doc/DEBIAN12_PYTHON312.md)**。
