# 构建：在项目根目录执行
#   docker build -f docker/Dockerfile -t fuck-polymarket-bot .
FROM python:3.12-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 默认纸面；实盘请用 compose command 或 docker run 覆盖
CMD ["python", "-m", "src.bot", "--mode", "paper", "--capital", "1000"]
