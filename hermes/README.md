# hermes_ds

大肥鯨（`../fat_whale_ds`）遷移到 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 嘅目標目錄。
2026-09-25 起。

## 現況

**架構同設計決定睇 [`ARCHITECTURE.md`](ARCHITECTURE.md)** —— 呢個 repo 唔係「用 Hermes
取代大肥鯨」，而係 Router + Hermes 兩層。

| 項目 | 狀態 |
|---|---|
| Docker image | ✅ `nousresearch/hermes-agent:latest`（2.74GB） |
| 資料目錄 | ✅ `data/` → 容器 `/opt/data`（= `HERMES_HOME`） |
| `SOUL.md` 人設 | ✅ 由 `build_soul.py` 生成，**實測語氣正確** |
| 模型 | ✅ `deepseek/deepseek-v4.1-flash`，Provider 自動判定為 OpenRouter |
| OpenRouter 金鑰 | ✅ 沿用大肥鯨同一條 |
| Telegram bot | ✅ `@deepseekgirl_beta_bot` 已連線（polling） |
| API server | ✅ `127.0.0.1:8642`，named conversation **實測隔離成功** |
| 唔要思考 | ✅ `agent.reasoning_effort: "none"` |
| 工具 | ✅ 由 20+ 削到 5 個（memory/skills/vision/web/video） |
| Router | ❌ **未寫** |
| BotFather Group Privacy | ❌ 未關（`can_read_all_group_messages = False`） |


## 仲要做嘅嘢

### 1. 開一隻新 bot（一定要新，唔可以共用大肥鯨條 token）

Hermes 一見到有人爭 `getUpdates` 會**主動踢走對方**（官方文件：*"Conflict recovery
still drops pending updates to terminate the competing getUpdates session"*）。
同一個 token 兩邊一齊行，Hermes 會打死大肥鯨。

1. Telegram 搵 **@BotFather** → `/newbot`
2. 改名（例：大肥鯨）同 username（要 `bot` 結尾）
3. 佢會回一段 token

### 2. （想要「每個串一個對話」就先做）開 Threaded Mode

@BotFather → `/mybots` → 你隻 bot → **Bot Settings → Threads Settings** → 開 **Threaded Mode**。

冇開嘅話 Hermes 會 log `The chat is not a forum`，然後靜靜哋跳過，唔會報錯。

### 3. 填 token

`data/.env` 入面將 `# TELEGRAM_BOT_TOKEN=` 嘅 `#` 拿掉，貼上 token。

### 4. 起

⚠️ **呢個目錄冇 `docker-compose.yml` 喇。** 成套系統（兩個腦 + 兩個 Router）
收埋喺 **repo 根** 嗰個 compose —— 由 2026-09-26 起。

```bash
cd C:\ds\fat_whale_ds          # ← repo 根，唔係 hermes/
docker compose up -d
docker compose logs -f hermes
```

見到 `Telegram adapter connected` 就……**其實唔應該見到** ——
`config.yaml` 係 `enabled: false`，Telegram 由 Router 揸。
見到即係兩邊爭 `getUpdates`（409）。

正確嘅成功訊號係 **Router** 嗰邊：`docker compose logs -f router`
見到 `Router 上線：@deepseek_girl_bot｜Hermes http://hermes:8642`。

## 兩個要記住嘅差異

**一、群組白名單取代咗「管理員在場」。**
大肥鯨原本嘅規則是「管理員目前喺唔喺呢個群」（`bot/group.py:56` `group_usable()`），
所以換誰拉佢入群都用到，你一走佢就跟住走。Hermes 冇呢個概念，只有靜態白名單 ——
即係以後要加群，要改 `config.yaml` 再重啟。呢個係遷移入面其中一個行為差異。

**二、`observe_unmentioned_group_messages: true` 係私隱上嘅改變。**
大肥鯨係「所有群組訊息寫入快取，**永不送進模型**」。
Hermes 開咗呢個之後，旁觀到嘅閒聊會喺你被 @ 之後一齊入 context。
想保守就改成 `false`。

## 檔案

| 檔案 | 入 git？ | 為咩 |
|---|---|---|
| `data/SOUL.md` | ✅ | 人設本體（slot #1）。兩部機靠 git 同步 |
| `data/config.yaml` | ✅ | 非機密設定 |
| `data/.env` | ❌ | 金鑰同 token |
| `data/{sessions,memories,skills,logs}/` | ❌ | 隨使用不斷變，入 git 只會製造衝突 |

Container 嘅定義**唔喺呢度** —— 喺 repo 根 `docker-compose.yml`（`hermes` service）。
原本呢個目錄嗰個 compose 已經刪咗（2026-09-26）—— 兩個並存會整出兩套
同名 container，而且兩邊都想揸 port 8642。

## SOUL.md 係點嚟

**唔係手抄** —— 用 script 由兩個來源抽，確保一字不漏：

```bash
cd ../fat_whale_ds && python -c "
from pathlib import Path
from dafeijing.core.persona import _SECURITY_RULE, _LENGTH_RULE, _MEDIA_RULE
body = Path('config/persona.md').read_text(encoding='utf-8').strip()
parts = [body, _LENGTH_RULE.strip(), _MEDIA_RULE.strip(), _SECURITY_RULE.strip()]
print('\n\n---\n\n'.join(parts))
" > hermes/data/SOUL.md
```

共 4,093 字元 = 人設本體 2,573 + 長度 324 + 媒體 447 + 安全界線 727。

⚠️ **最易搬漏嘅就係寫死喺 `core/persona.py` 嗰三節** —— 佢哋唔喺 `persona.md` 入面。
`_SECURITY_RULE` 尤其重要，係反 prompt injection 嘅唯一防線。

## 更新

Docker 版**唔支援 `hermes update`**：

```bash
docker compose pull && docker compose up -d
```
