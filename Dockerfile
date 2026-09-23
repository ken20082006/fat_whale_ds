FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY dafeijing/ ./dafeijing/
COPY config/persona.example.md ./config/persona.example.md

# 資料與日誌掛載出來，容器重建不會遺失
RUN mkdir -p data logs && useradd -m whale && chown -R whale:whale /app
USER whale

# persona.md 與 .env 於執行期掛載，不進映像檔
CMD ["python", "-m", "dafeijing.main"]
