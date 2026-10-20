FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    NODE_ENV=production \
    DSH_HOME=/opt/dsh

WORKDIR /app

# ⚠️ **ffmpeg 唔可以刪。** 唯一用途係真 GIF → MP4（`router/videos.py`）。
#
# 舊設計係「真 GIF 交 xiaomi 經 image_url 睇」，但實測佢**作出郁動**：
# 一條「藍方塊固定、紅圓水平向右移」嘅測試 GIF，佢答「兩個都係垂直移動」。
# 轉做 MP4 行影片條之後，同一個模型答得完全正確，而且快 5 倍。
#
# python3-yaml 係 dsh entrypoint 砌 persona 用（SOUL.md 有引號同換行，
# 唔可以 sed）；curl 係裝 Node 用。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates tzdata ffmpeg curl xz-utils \
    && rm -rf /var/lib/apt/lists/*

# ── Node.js（dsh 要）──────────────────────────────────────
# node:24 嘅 binary 直接由官方 tarball 攞，唔用 NodeSource（少個 repo key）。
#
# ⚠️ 用 `uname -m` 而唔係 `dpkg --print-architecture` —— Dockerfile 嘅 RUN
# 預設係 `/bin/sh`（dash），`case` 喺一行接續嘅寫法好易撞語法錯。
ARG NODE_VERSION=24.22.0
RUN set -eux; \
    arch="$(uname -m)"; \
    if [ "$arch" = "x86_64" ]; then node_arch=x64; \
    elif [ "$arch" = "aarch64" ]; then node_arch=arm64; \
    else echo "unsupported arch: $arch" >&2; exit 1; fi; \
    curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.xz" \
        | tar -xJ -C /usr/local --strip-components=1; \
    node --version; \
    npm --version; \
    corepack enable

# ── DeepSeek Harness ──────────────────────────────────────
# ⚠️ **版本釘死。** 0.2.1-rc.2 係 npm `next` tag 嗰個 ——
# `npm i @deepseek-ai/dsh` 會裝到 `latest`（0.0.1-rc.1），功能完全唔同。
RUN npm install -g --no-audit --no-fund @deepseek-ai/dsh@0.2.1-rc.2 \
    && npm cache clean --force

COPY requirements.txt ./
# ⚠️ PyYAML 要喺 **slim image 自己個 Python** 度裝 —— image 有
# `/usr/local/bin/python3`（3.12）蓋過系統 `/usr/bin/python3`，
# 而 apt 嘅 `python3-yaml` 裝落系統嗰個，import 唔到。
COPY dsh/requirements-dsh.txt ./
RUN pip install -r requirements.txt -r requirements-dsh.txt

COPY dafeijing/ ./dafeijing/
COPY dsh/profile/ /opt/dsh-src/profile/
COPY dsh/entrypoint.sh /usr/local/bin/dsh-entrypoint
RUN chmod +x /usr/local/bin/dsh-entrypoint

# ⚠️ **profile 喺 build 時起好。** dsh 第一次見到未知 profile 名會由
# template 初始化，但嗰陣先裝 out-of-tree plugin 就要開機時跑 pnpm。
# 呢度依賴係空，所以冇嘢要裝，只係砌好個目錄結構。
RUN mkdir -p "${DSH_HOME}/profiles/fatwhale-sdk" \
    && cp /opt/dsh-src/profile/package.json "${DSH_HOME}/profiles/fatwhale-sdk/package.json" \
    && cp /opt/dsh-src/profile/pnpm-workspace.yaml "${DSH_HOME}/profiles/fatwhale-sdk/pnpm-workspace.yaml" \
    && cp /opt/dsh-src/profile/cordis.patch.yml "${DSH_HOME}/profiles/fatwhale-sdk/cordis.patch.yml"

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

# ⚠️ **唔可以轉 `whale` 用戶。** dsh 要寫 $DSH_HOME（掛載自
# `./dsh/data`，uid 10000 / root 睇 host 權限），而且 compose 已經
# 將 router 設成 root（見 compose 嘅 `user: "0:0"`）。
# 呢度唔 USER，改由 compose 話事。

CMD ["python", "-m", "dafeijing.router.main"]
