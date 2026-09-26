FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ⚠️ **ffmpeg 唔可以刪。** 唯一用途係真 GIF → MP4（`router/videos.py`）。
#
# 舊設計係「真 GIF 交 xiaomi 經 image_url 睇」，但實測佢**作出郁動**：
# 一條「藍方塊固定、紅圓水平向右移」嘅測試 GIF，佢答「兩個都係垂直移動」。
# 轉做 MP4 行影片條路之後，同一個模型答得完全正確，而且快 5 倍。
#
# 大肥鯨年代刻意唔要 ffmpeg（「不需要在本機解碼影片」）—— 嗰個決定係基於
# 「影片一律外包」。GIF 係例外，所以呢個例外要落地解碼。
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY dafeijing/ ./dafeijing/

# 維運腳本也放進去，這樣在容器裡就能跑自我檢查，不必另外裝 Python：
#   docker compose exec router python scripts/router_smoke.py
COPY scripts/ ./scripts/

# 掛載點（見 repo 根 docker-compose.yml）。**兩隻 bot 各有自己嘅 CWD**：
#
#   大肥鯨      → /app          相對路徑 data/、logs/、hermes/data/cache/videos
#   貝爾法斯特  → /app/belfast  相對路徑 data/、logs
#
# 所以兩層目錄都要預先建好 —— compose 嘅 `working_dir` 指去唔存在嘅目錄
# 會直接起唔到。
RUN mkdir -p data logs hermes/data/cache/videos belfast/data belfast/logs \
    && useradd -m whale && chown -R whale:whale /app
USER whale

# 兩個 service 都係同一個 image、同一個 CMD，只係 working_dir 唔同
# —— 角色嘅分別喺 Hermes 嗰邊嘅 SOUL.md，唔喺呢度。
CMD ["python", "-m", "dafeijing.router.main"]
