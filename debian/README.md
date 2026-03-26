# Debian 环境安装脚本

## 脚本说明

| 文件 | 作用 |
|------|------|
| `setup_env.sh` | 在 Debian（推荐 12 bookworm）上安装编译依赖、**pyenv**、**Python 3.12.x**，并在项目根目录创建 `.venv` 并执行 `pip install -r requirements.txt` |

稳定仓库没有 `python3.12` 软件包；`polymarket-apis` 需要 **Python ≥ 3.12**，因此用 pyenv 从源码安装 Python 3.12。

`apt` 阶段会一并安装 **`curl`、`wget`、`git`**（及 Python 编译所需开发库），便于拉取 pyenv、下载依赖与日常使用。

## 用法

在项目根目录执行：

```bash
chmod +x debian/setup_env.sh
./debian/setup_env.sh
```

首次编译 Python 可能需 **5～15 分钟**，请保持网络畅通。

## 选项

```bash
./debian/setup_env.sh --help

# 只安装 apt 编译依赖（不装 pyenv / 不建 venv）
./debian/setup_env.sh --system-only

# 装 pyenv + Python，但不创建 .venv、不 pip install
./debian/setup_env.sh --skip-venv
```

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `PYTHON_VERSION` | `3.12.8` | pyenv 安装的版本 |
| `SKIP_APT=1` | 关闭 | 已装过编译依赖时跳过 `apt-get` |
| `SKIP_PYENV=1` | 关闭 | 假定 pyenv 已安装，仅做后续步骤时需配合手动配置 PATH |

## 验证

```bash
source .venv/bin/activate
python --version   # Python 3.12.x
pip install -r requirements.txt   # 应能安装 polymarket-apis
```

更详细的背景与替代方案见 **[doc/DEBIAN12_PYTHON312.md](../doc/DEBIAN12_PYTHON312.md)**。
