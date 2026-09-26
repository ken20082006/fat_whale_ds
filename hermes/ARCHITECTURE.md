# Router 架構

2026-09-26 定案。大肥鯨唔係「被 Hermes 取代」，而係**拆剩個殼做 router，換個腦**。

> **2026-09-26 晚更新**：呢份文件原本只講大肥鯨一隻 bot。
> 而家係**兩隻 bot 共用同一份 Router 代碼**（大肥鯨 ＋ 貝爾法斯特），
> 成套嘢收埋喺 repo 根**一個 `docker-compose.yml`**、四個 service。
> 見下面「架構」。貝爾法斯特嘅人設層喺 [`../belfast/`](../belfast/README.md)。

## 為什麼要 router

Hermes 嘅 session key 係 `agent:main:telegram:group:<chat_id>:<thread_id>`，
而 `thread_id` **只喺 forum 群嘅 Topic 或 DM topic 才有值**（`adapter.py:946-953`）。
我哋五個群一個都冇開 Topics，所以 Hermes 原生做唔到「每條引用串一個對話」。

Plugin 系統亦封死咗：官方 hook 合約明文寫 *"**Observer-only** … grants **no adapter
handles or platform actions"*，`pre_command` 更寫明 *"returns IGNORED in v1"*。
即係冇任何 hook 位可以改 session key。

**所以改由外面控制。**

## 架構

**四個 service，一個 compose**（repo 根 `docker-compose.yml`）——
兩個腦 ＋ 兩個外殼，同一個 network，Router 用 service name 叫 Hermes：

```
Telegram ──┬─► router ────► hermes   ┐
           │   (外殼)       (腦)      │
           └─► router-belfast ─► belfast ┴─► OpenRouter / xAI
               (外殼)          (腦)
```

```
Telegram
   │
   ▼
┌─────────────────────────────────────┐     ┌─────────────────────────────────────┐
│  router（大肥鯨拆剩嘅殼）             │     │  router-belfast（同一份代碼）         │
│  • Telegram polling / 授權 / 節流     │     │  • 同上，但完全獨立嘅一套設定          │
│  • 解析引用串 → 搵到串根               │     │  • bot token / DB / log / 媒體目錄    │
│  • conversation = grp:<chat_id>:<串根>│     │    一律分開 —— 兩隻 bot 唔可以互相繼承 │
│  • 逐句標註 [名|user_id]              │     │  • 角色名由 FW_SELF_NAME 帶入         │
│  • 注入當前發言者嘅 per-user 筆記      │     │    （見 settings.self_name）          │
│  • 落媒體檔（真 GIF 先轉 MP4）         │     │                                      │
└──────────┬──────────────────────────┘     └──────────┬──────────────────────────┘
           │  POST http://hermes:8642/v1/responses      │  POST http://belfast:8642/...
           │  {"input": "...", "conversation": "grp:..."}
           ▼                                            ▼
┌─────────────────────────────────────┐     ┌─────────────────────────────────────┐
│  hermes（腦）                         │     │  belfast（腦）                       │
│  • SOUL.md 人設（大肥鯨）              │     │  • SOUL.md 人設（貝爾法斯特）          │
│  • 每個 conversation 一條獨立歷史      │     │  • 同樣嘅架構，唔同嘅角色同 model      │
│  • web search / vision / video_analyze│     │                                      │
│  • 模型呼叫（OpenRouter / deepseek）   │     │  • 模型呼叫（xAI / grok）             │
└──────────┬──────────────────────────┘     └──────────┬──────────────────────────┘
           │  回覆                                       │
           ▼                                            ▼
     Router 渲染 → 送回 Telegram
```

⚠️ **容器入面 `127.0.0.1` 係自己** —— `FW_HERMES_URL` 一定要由
`http://127.0.0.1:8642` 蓋成 `http://hermes:8642`（compose 嘅 `environment:` 做咗）。
主機 port `127.0.0.1:8642` / `:8643` 仍然開住，但純粹為咗主機側除錯。

**舊寫法**（Router 跑喺主機、只有一個腦）已經退役 —— 唔使再靠看守程序
或者某個 session 開住個 shell，`restart: unless-stopped` 自己處理。

## 已實測

```
chain-a 記「藍色」 → 問 chain-a → 藍色 ✅
                    問 chain-b → 紅色 ✅
```

兩條對話完全隔離，**跨容器重啟都仲記得**。

## 資產分配

### Router 保留（由大肥鯨搬，大部分唔使改）

| 檔案 | 角色 |
|---|---|
| `router/chain` → `core/chain.py` | ⭐ 引用串解析 → 直接決定 `conversation` 個名 |
| `core/access.py` | 授權、邀請碼、群組白名單 |
| `core/ratelimit.py` | 節流 |
| `core/stickers.py` | 貼圖庫、`[[貼圖:N]]` 解析 |
| `render/` | Markdown → TG HTML、長訊息分拆 |
| `router/ui.py` | 送出、typing（原 `bot/ui.py`，2026-09-26 搬入 `router/`）|
| `store/` | SQLite（`group_cache`、`memory_notes`、`usage_log`…）|
| `core/memory.py` | per-user 筆記抽取（見下）|
| `llm/` | ⚠️ **冇掉走** —— 記憶抽取同 Jev 決策閘仍然經 OpenRouter 直打 |

### 交畀 Hermes

人設、對話歷史、模型呼叫、web search、vision、**影片同真 GIF 分析**。

### 掉走（2026-09-26 清走）

`core/chat.py`、`core/persona.py`（三節規則搬去 `hermes/persona_rules.py`）、
`core/security.py`、`core/webfetch.py`、`core/tuning.py`、`core/debounce.py`、
`core/maintenance.py`、`dafeijing/bot/` 成個目錄。

其後**再清一批**（同日夜晚，見下面「真 GIF」）：
`core/media.py` 嘅 `describe_video` / `remember_note` / `get_cached_note` /
`VideoNote` / `_DELEGATE_PROMPT`，同 `router/gif.py` 成個檔。

## 四個關鍵決定

### 1. 每條引用串 = 一個 conversation

```
有引用 → conversation = f"grp:{chat_id}:{串根 message_id}"   # 續返嗰條
冇引用 → conversation = f"grp:{chat_id}:{自己 message_id}"   # 開新一條
```

`chain.py` 已經有 `resolve()` 追串根（仲有 `reply_to_id` 必須用 `COALESCE`
嗰個血淚修正，令一段對話唔會爆成 115 個 session）。直接複用。

#### 「同一串」嘅規則

**只有引用本鯨嘅回答先算同一串。**

```
用戶1 @bot              → 開新對話 A
bot 回答 R1（記住 R1 屬於 A）
用戶2 引用 R1           → 同一條對話 A ✓
用戶3 引用 用戶2         → 唔算 ✗ —— 開新一條
```

所以**唔使追成條引用鏈**。只需要記住本鯨每則回覆屬於邊條對話 ——
Router 將對話名寫入 `group_cache.conversation`（只有 bot 發嘅訊息有值），
別人引用本鯨嗰則時查返出嚟就得。查唔到（引用嘅唔係本鯨，或者冇紀錄）
就開新串，用觸發嗰則自己嘅 `message_id` 做名。

咁亦代表**每次只需要送觸發嗰一句**：一條對話入面除咗觸發訊息就係本鯨
自己嘅回覆，Hermes 已經有齊歷史，唔使重送，亦冇嘢會漏。

`group_cache.conversation` 同 `reply_to_id` 一樣用 `COALESCE` ——
本鯨自己嗰則之後會經 `cache_from_update()` 再寫一次（冇 conversation），
覆蓋咗就接唔返條對話。

#### 為什麼唔可以靠 Hermes 原生

Hermes 群組 session key 係 `group:<chat_id>:<user_id>` —— **每人一條，
冇 TTL、冇自動重置**，所以每次 @ 都係續同一條對話。實際觀察到：

```
agent:main:telegram:group:-5303991392        ← 一條，永遠同一條
agent:main:telegram:dm:216587605:1976034     ← DM 有 topic 所以獨立，但群組冇
```

DM 之所以分得開，係因為 BotFather 開咗 Threaded Mode；群組要 `is_forum`
才有 `message_thread_id`，而我哋五個群都冇開 Topics。

#### ⚠️ 一定要連自己嘅回覆都寫入快取

大肥鯨 `bot/group.py:300-307` 有做：Telegram 唔會將 bot 自己發嘅訊息
回傳畀 bot，所以要手動補快取。冇做嘅話，當有人「回覆本鯨」時追唔到串根，
嗰句會被當成**新串** —— 對話即刻斷開。呢個係最容易漏嘅一步。

### 2. 逐句標註發言者

一條串幾個人講嘢，唔標註 Hermes 會當同一個人。格式沿用 Hermes 自己嗰套：

```
[陳大文|216587605]
今日隻船係咪要改期？

[小明|123456]
我睇下先
```

`SOUL.md` 要加一節解釋呢個慣例（**未加，待辦**）。

### 3. per-user 記憶由 Router 自己做

Hermes 嘅 `USER.md` / `MEMORY.md` 係 **profile 全域**
（`memory_tool_store.py:212`：一個 Hermes home 只有一份），冇任何 per-sender 概念。
群組十個人會撈埋一份，所以**唔用 Hermes 嗰套**。

Router 保住大肥鯨嘅 `memory_notes`（`user_id` + `scope`），連帶保留：
Jev 記憶閘（`memory.py:188`）、自動抽取、定期濃縮、scope 隔離。

每次請求前，將**當前發言者**嘅筆記注入。

### 4. 真 GIF：轉做 MP4，行返影片條路（2026-09-26 改）

**Hermes 收唔到真 GIF：**

```python
_VIDEO_MIME_TYPES = {".mp4", ".webm", ".mov", ".avi", ".mkv", ".mpeg", ".mpg"}
```

`.gif` 唔喺清單，而且 Hermes 一律用 `video_url` 送。

**原本嘅做法**（Router 自己交 `xiaomi` 經 `image_url`）**已推翻** ——
`xiaomi` 收得落 GIF，但**作出郁動**。實測一條「藍方塊固定左上角、
紅圓水平由左移向右」嘅測試 GIF：

| model | content type | 結果 |
|---|---|---|
| `xiaomi/mimo-v2.6-flash` | `image_url` | 有回應，但答「兩個都係**垂直**移動」❌ |
| `xiaomi` | `video_url` | HTTP 422 |
| `bytedance-seed/seed-2.0-mini` | `image_url` | 回空白 |
| `seed-2.0-mini` | `video_url` | HTTP 400 |

即係**冇一個 model 真係睇得明 GIF 嘅郁動**。

**而家**：Router 用 ffmpeg 將真 GIF 轉做 MP4，再當普通影片交畀 Hermes。
同一個 model（`seed-2.0-mini`，經 `video_url`）收到轉出嚟嘅 MP4 就答得
**完全正確**（「藍色正方形始終停在左上角沒有變化，红色圆形向畫面右侧移动」），
而且 15 秒變 3 秒。

- 代價：Router image 要 **ffmpeg**（+~100MB）。呢個推翻咗大肥鯨
  「本專案不需要 ffmpeg」嗰個決定 —— 嗰個係基於「影片一律外包」，GIF 係例外。
- 實作喺 `router/videos.py`：`should_send_video` 收埋真 GIF，
  `_gif_to_mp4` 負責轉檔（**落真檔而唔用 pipe** —— MP4 要 seek 返轉頭補
  `moov` atom；經 stdout 就要 `frag_keyframe`，出嚟係 fragmented MP4）。
- **Router 而家完全唔再打媒體 model**，`router/gif.py` 已刪。

## 成本

| | 每次呼叫 input tokens |
|---|---|
| 大肥鯨 | ~4,000 |
| **Hermes** | **~16,800** |

約 **4 倍**（已削工具之後嘅數）。金額細（約 $0.0017/則，按 `$0.1/百萬`），
但係 4 倍。呢個係 `TODO.md:46-48` 當初拒絕接 agent 框架嘅理由，已實測。

## 設定

`config.yaml` 已改：

| 項目 | 值 | 為咩 |
|---|---|---|
| `agent.reasoning_effort` | `"none"` | 唔要思考，要答得快。⚠️ 一定要加引號 |
| `display.show_reasoning` | `false` | 移除思考過程貼出 |
| `platform_toolsets.cli` | memory, skills, vision, web, video | 由 20+ 削到 5 個 |
| `auxiliary.vision.model` | `bytedance-seed/seed-2.0-mini` | 影片分析（`video_analyze` fallback video→vision） |

`.env` 加咗 `API_SERVER_ENABLED=true`。`API_SERVER_KEY` 由 Hermes 自己生成
—— **唔好喺 `.env` 再寫一次**，兩條同名 key 邊條生效係睇實作。

## 未解決

- **`model` 淨低嘅用途只剩用量標籤**（`router/handlers.py` 寫入 usage 表）。
  Router 唔用佢答任何嘢。同類嘅 `model_vision` 已經刪（零呼叫者）。
- **`FW_GIF_DELEGATE_MODEL` / `FW_VIDEO_DELEGATE_MODEL` 已經冇用** ——
  Router 唔再打媒體 model。env 檔入面嗰兩條線已刪。
- 群組白名單取代咗大肥鯨「管理員在場」嘅規則
- `SOUL.md` 新增嘅「查證」「記憶」兩節只做過抽樣實測

**已解決（原本列喺度）**：影片轉發 ✅（`router/videos.py` 落檔 ＋
`video_note` 畀容器內路徑）、指令 ✅（15 個已搬，見 `router/commands.py`）。

---

# 進度

## 2026-09-26 晚：第二隻 bot ＋ 收歸一個 compose

| 做咗 | 邊度 |
|---|---|
| 第二個人設（貝爾法斯特）| `belfast/` |
| 四個 service 一個 compose | repo 根 `docker-compose.yml` |
| Router 設定化（`FW_SELF_NAME`）| `settings.self_name` |
| 貼圖標記收單括號 | `core/stickers.py` |
| 真 GIF 轉 MP4，Router 唔再打媒體 model | `router/videos.py` |
| 掃走死代碼 | `core/media.py` −170 行、`settings.py` −76 行、刪 `router/gif.py` |

## 已完成（2026-09-26）

Router 喺 `fat_whale_ds` 嘅 `router-prototype` branch：

| 檔案 | 做咩 |
|---|---|
| `dafeijing/core/chain.py` | `conversation_of()` —— 查本鯨嗰則屬於邊條對話 |
| `dafeijing/router/conversation.py` | 對話命名、`[名\|id]` 標註 |
| `dafeijing/router/hermes.py` | API client |
| `dafeijing/router/memory.py` | ⭐ per-user 筆記注入 |
| `dafeijing/router/gif.py` | ⭐ 真 GIF 例外 |
| `dafeijing/router/services.py` | `RouterServices` |
| `dafeijing/router/handlers.py` | 群組／私聊 handler |
| `dafeijing/router/app.py` | 三個 handler 嘅組裝 |
| `dafeijing/router/main.py` | 入口 |
| `scripts/router_smoke.py` | 上線前檢查 |
| `scripts/run_router.ps1` | 看守程序（讀 `.env.router`） |

**實測**：Router 以 `@deepseekgirl_beta_bot` 上線成功，接咗 Hermes，
喺 `test bot` 群試過 —— 兩條串各自一條對話（`:75` 同 `:81`）。

## per-user 記憶

**Hermes 做唔到呢件事**：`USER.md` / `MEMORY.md` 係 profile 全域
（`memory_tool_store.py:212`：一個 Hermes home 只有一份），冇 per-sender
概念。群組十個人嘅事實會撈埋一份。

所以 Router 繼續用大肥鯨嗰套 `memory_notes`（`user_id` + `scope`）：

- **注入**：每次請求前，將**當前發言者**嘅筆記附喺訊息前面，包 `<筆記>` 標記
- **抽取**：回覆之後背景抽（`MemoryExtractor.schedule`），唔阻塞
- **scope**：`private` / `group:<chat_id>` —— 私聊講過嘅嘢永遠唔會喺群組出現
- **冇筆記要明講** —— 大肥鯨留白出過事：模型抓咗同一條串另一個人嘅名充數

⚠️ **抽取走 OpenRouter 直打，唔經 Hermes**。呢個係 utility 工作：
用 Hermes 做要成 11k tokens 一次，用 OpenRouter 係千幾。對話本身
仍然 100% 經 Hermes。

## 真 GIF

**Router 唔再自己睇真 GIF** —— 改為用 ffmpeg 轉做 MP4，再當普通影片
交畀 Hermes。完整推導同實測數據見上面「四個關鍵決定 §4」。

分辨方法冇變：`source == "animation"` 且**冇** `clip_file_id` 就係真 GIF
（有 clip 嗰啲係 Telegram 自己轉過嘅 MP4）。但兩者**而家行同一條路**，
所以 `should_send_video` 唔再排除真 GIF。

⚠️ 舊嘅 `media_notes` 快取表**已經冇人寫／讀** —— `describe_video` 全家
連同 `router/gif.py` 一齊刪咗。張表刻意留住（遷移唔值得，而且無害），
schema 有註釋講明。

## 圖片同貼圖

**靜態圖轉發去 Hermes 嘅 vision**，用 Responses 格式嘅 inline image：

```json
{"input": [{"role": "user", "content": [
  {"type": "input_text", "text": "..."},
  {"type": "input_image", "image_url": "data:image/jpeg;base64,..."}
]}]}
```

相片本身仍然係本機處理（下載、縮圖、鋪白底、轉 JPEG），冇多花一蚊。

| 來源 | 送唔送 |
|---|---|
| `photo` | ✅ |
| `sticker` | ✅（`.tgs` 動態貼圖送縮圖） |
| `sticker_motion` | ✅（影片貼圖送縮圖，好過完全睇唔到） |
| `animation` | ❌ 真 GIF → 轉 MP4 交 Hermes（見上面「真 GIF」）|
| `video` / `video_note` | ❌ 落檔交 Hermes 嘅 `video_analyze` |

**貼圖送出**：清單注入 + 解析 `[[貼圖:編號]]`。清單放喺**請求**度而唔係
`SOUL.md`（Router 改唔到 Hermes 嘅 system prompt），但**每條對話只附一次**
—— 每則都附嘅話清單會不斷累積入 Hermes 嘅對話歷史，越傾越貴。
送出嘅貼圖同樣補快取，否則別人引用嗰張貼圖時條串會斷。

## 存取閘

**跟返大肥鯨嘅分野**（`bot/private.py:32` 有查，`bot/group.py:227` 冇查）：

| | 規則 |
|---|---|
| **群組** | 只靠 `group_usable` —— 「管理員在唔在個群」。**唔逐個人查** |
| **私聊** | `is_active`（邀請碼）或者管理員 |

⚠️ **群組千祈唔好加個人閘。** 我一度加咗，結果 21 個 pending 用戶
全部冇反應 —— 但佢哋本來用得。大肥鯨嘅群組設計係「主人喺度就得」，
換誰拉 bot 入群都用得到；主人一走 bot 亦跟住走。

私聊就繼續要邀請碼。唔應嘅人靜靜哋唔回（連拒絕都唔回，免得變成
回音壁），同大肥鯨唔同嘅係大肥鯨會提示邀請碼 —— Router 未有指令，
所以暫時冇嗰句。

## DB 分開

Router 用自己一個 DB（`FW_DB_PATH`，預設 `data/router.db`）—— 試用期兩隻
bot 同時跑，共用同一個 SQLite 有鎖定風險（README:438-453 記錄過一次
`database disk image is malformed`）。

但咁樣 Router 一開波係空白，所以有 `scripts/seed_router_db.py` 由大肥鯨搬
`stickers` / `memory_notes` / `users` 過去。唔搬 `group_cache` / `sessions` /
`messages`（大肥鯨自己嘅對話狀態），亦唔搬 `runtime_settings`。

## 部署設定（實測撞到嘅嘢）

### 1. API server 要綁 0.0.0.0（容器內部）

Docker 嘅 port mapping 打去容器嘅 eth0，而 Hermes 預設綁 127.0.0.1 —— 
**映射唔到入去**（實測 curl 回 `http_code=000`）。要喺 `data/.env` 加：

```
API_SERVER_HOST=0.0.0.0
```

對外仍然只綁 `127.0.0.1:8642`（見 `docker-compose.yml`），唔會出公網。

### 2. Hermes 嘅 Telegram platform 要關

Router 揸 bot token，Hermes 唔可以同時 polling 同一個。

```
hermes config set platforms.telegram.enabled false
```

### 3. ⚠️ API server 係冇沙盒嘅

Hermes 自己警告：

> API server is network-accessible (0.0.0.0) AND the terminal backend is
> 'local' (unsandboxed). Agent work dispatched through this endpoint runs as
> the host user with **full terminal/file access**.

即係任何掂得到 8642 嘅人（有 key）都可以喺容器入面執行指令。
而家 host port 只綁 127.0.0.1，所以只有本機 process 掂得到。要再收窄就：
- 設 `terminal.backend: docker`（沙盒，但變 docker-in-docker）
- 或者用 firewall 鎖死個 port

### 4. ⚠️ Docker Desktop bind mount 上 SQLite 唔可以行 WAL

Hermes 開機自己偵測到並轉做 `journal_mode=DELETE`：

> database directory is on a cross-VM filesystem (virtiofs/9p — typical for
> Docker Desktop). SQLite WAL shared-memory is not coherent across the VM
> boundary and **can silently corrupt the database**.

**呢個正正就係大肥鯨當年 `database disk image is malformed` 嘅成因**
（見 fat_whale_ds README:438-453）。Hermes 自動處理咗，但值得知。

### 5. ⚠️ Router 容器要行 root（`user: "0:0"`）

搬入 Docker 之後 `router-belfast` 起唔到：

```
sqlite3.OperationalError: unable to open database file
```

因為 Router 要寫入三個 host 目錄，**擁有者唔同**：

| 目錄 | 擁有者／權限 |
|---|---|
| `./data`、`./logs`、`./belfast/logs` | root:root **777** |
| `./hermes/data/cache/videos` | **uid 10000** / 755 |
| `./belfast/data` | **uid 10000** / 700 |

後兩者係 **Hermes 容器自己建**嘅（Hermes 內部行 uid 10000），
而 700 連 `ls` 都唔得。Router image 預設嘅 `whale`（uid 1000）兩邊都撞。

**行 root 係最簡單而又唔使 chown 嚟 chown 去嘅做法** —— 兩隻 bot 嘅檔案
擁有者本身唔一致，硬砌一個 uid 只會兩邊都撞。呢個容器冇對外開任何 port
（只係出去 polling Telegram），所以風險有限。

### 6. ⚠️ `FW_HERMES_URL` 一定要蓋成 service name

容器入面 `127.0.0.1` 係**自己**。`.env.router` 嗰句
`http://127.0.0.1:8642` 喺主機係啱嘅，喺容器就死症 ——
compose 用 `environment:` 蓋成 `http://hermes:8642` / `http://belfast:8642`。
**唔好刪嗰兩行。**

### 7. Router image 要 ffmpeg；CWD 決定繼唔繼承 `.env`

- **ffmpeg**：真 GIF → MP4 用（見「真 GIF」）。唔好因為舊註釋
  「本專案不需要 ffmpeg」而刪走。
- **CWD**：`settings.py` 寫死 `env_file=".env"`，係 **CWD 相對**。
  - `router`：`working_dir: /app` → 讀到 repo `.env`（大肥鯨自己嗰份）
  - `router-belfast`：`working_dir: /app/belfast` → 嗰度冇 `.env`
    → **零繼承**，每個 `FW_` 都要喺 `belfast/.env.router` 填齊

  呢個係刻意嘅：兩隻 bot 係兩套嘢，唔應該透過 `.env` 互相影響 ——
  尤其 token 同 API key，繼承到就大件事。順帶 `PYTHONPATH=/app`，
  因為 `working_dir` 唔係 repo 根時 `-m dafeijing.router.main` 會搵唔到 package。

