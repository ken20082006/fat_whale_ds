# 大肥鯨

DeepSeek 的擬人化 Telegram Bot。私人邀請制，只有拿到邀請碼的人能用。

不是公開服務，也不是框架 —— 它是一隻養在自家 VPS 上的鯨魚。

---

## 架構：Router + Hermes 兩層（2026-09-26 起）

```
Telegram → Router（呢個 repo）→ Hermes API → 回覆
```

| | 做咩 |
|---|---|
| **Router**（`dafeijing/router/`） | 揸 Telegram bot token。引用串分對話、媒體、貼圖、per-user 記憶、指令 |
| **Hermes** | 個腦。人設（`SOUL.md`）、對話歷史、模型呼叫、工具（聯網、vision、video_analyze） |

**為什麼要分兩層**：本來想直接用 Hermes 取代。但 Hermes 嘅群組 session key
係「每人一條、冇 TTL」，做唔到「每條引用串一條對話」；plugin 又係
observer-only，冇 hook 位改。所以由外面控制 `conversation` 個名。

**完整設計同踩過嘅坑**：`C:\ds\hermes_ds\ARCHITECTURE.md`

**舊版（純大肥鯨，自己打 OpenRouter）**：`legacy-fatwhale` branch。

---

## 特色

**對話**

- **每條引用串 = 一條獨立 Hermes 對話。** 唔引用任何嘢 @ 本鯨就開新嘅；
  引用本鯨嘅回覆就續返嗰條
- 引用其他人嘅訊息時，**成條引用串**（由舊到新）都會附上，唔會只睇最尾嗰則
- 三層背景資料：**群組概況** → **自己嘅筆記** → **被 @ 到嗰個人嘅筆記**
- 自動記憶：會自己從對話記住關於你嘅事，不必手動 `/remember`
- 筆記累積到 25 則會自動濃縮到 12 則，不會無限膨脹
- 回覆以 Telegram HTML 輸出，長文自動分段且不切斷程式碼區塊

**媒體**

- **相片、貼圖**：直接轉發去 Hermes 嘅 vision 睇。相片嘅下載、縮圖、
  鋪白底、轉 JPEG 全部喺本機做，冇多花一蚊
- **真 GIF**：**Router 自己睇**。Hermes 嘅 `video_analyze` 收唔到 ——
  佢個支援清單冇 `.gif`，而且一律用 `video_url` 送，實測會 HTTP 400。
  只有 `xiaomi/mimo-v2.6-flash` 經 `image_url` 睇得到（十個模型實測）
- **影片**：Router 落一個檔，交個路徑畀 Hermes，由佢自己叫 `video_analyze`。
  Hermes 收唔到 data URL（`input_file` 會 400）
- **睇唔到就直接唔睇。** 外包失敗就當作沒有這個媒體，**不抽格充數**
- 媒體描述按媒體嘅穩定識別碼快取 —— 同一條片再傳唔會重新外包
- **本專案不需要 ffmpeg** —— 冇抽格這回事

**群組**

- 只有被 @ 或被回覆時才回應，其餘訊息不讀不答
- **可用條件是「管理員在不在這個群組裡」**，不是「誰把 bot 加進來」。
  你在的群就能用，誰拉它進去都無所謂；你退出，它也跟著退出
- **群組概況**：累積出「這個群體本身」的樣貌（主題、氣氛、慣例），
  不屬於任何一個人
- 所有群組訊息寫入 `group_cache`（保留 72 小時），只為還原引用串；
  **除了被指名的那條串，其餘內容永不送進模型**

**授權**

| 路徑 | 條件 |
|---|---|
| 群組 | **管理員在該群組裡**。群內任何成員都能 @ 它 |
| 私聊 | **必須持有邀請碼**。群組成員沒有私聊權限 |

邀請碼一次性、預設五分鐘失效，用 Telegram 深連結一鍵綁定。
`/allowgroup` 是唯一會留下永久放行的方法，只有管理員能下。

**成本**

- 完整記錄每人每次嘅 token 與估算費用
- 預設只記錄不封鎖，跑一段時間後再決定配額
- ⚠️ **費用係估算嘅** —— Hermes 嘅 API 只回 tokens 唔回 cost。
  價錢喺 `settings.py` 嘅 `hermes_*_price`，換模型要跟住改

---

## 快速開始

### 1. 建立 bot

在 Telegram 找 [@BotFather](https://t.me/BotFather)：

```
/newbot        # 建立 bot，取得 token
/setprivacy    # 選 Disable —— 必須關閉，否則群組引用鏈無法回溯
```

### 2. 起 Hermes

Hermes 係個腦，要另外起。見 `C:\ds\hermes_ds\`：

```bash
cd hermes_ds
docker compose up -d
```

`config.yaml` 入面 `platforms.telegram.enabled` **一定要係 false** ——
Telegram 由 Router 揸，兩邊一齊 polling 同一個 token 會 409，
而且 Hermes 會主動踢走爭 `getUpdates` 嘅對手。

要記低 `data/.env` 嘅 `API_SERVER_KEY`（Router 要用）。

### 3. 安裝 Router

```bash
git clone https://github.com/ken20082006/fat_whale_ds.git
cd fat_whale_ds

py -3.12 -m venv .venv                 # Windows
# python3.12 -m venv .venv             # Linux

.venv/Scripts/python -m pip install -r requirements.txt
```

### 4. 設定

```bash
cp .env.router.example .env.router
```

填好入面四樣：`FW_TELEGRAM_BOT_TOKEN`、`FW_HERMES_KEY`（上面嗰條
`API_SERVER_KEY`）、`FW_OPENROUTER_API_KEY`（GIF 外包同記憶抽取用）、
`FW_ADMIN_USER_IDS`。

### 5. 啟動

```bash
.venv/Scripts/python scripts/router_smoke.py     # 上線前自我檢查
powershell -ExecutionPolicy Bypass -File scripts\run_router.ps1
```

看到「Router 上線」就成功。在 Telegram 對它說 `/id` 確認身份，
再用 `/issue` 產生第一張邀請碼。

---

## 指令

| 指令 | 說明 |
|---|---|
| `/new` | 開新對話（**只有私聊** —— 群組每條引用串本來就係一條） |
| `/context` | 睇下各場合嘅筆記數量 |
| `/remember <內容>` | 寫入這個場合的長期記憶 |
| `/forget` | 清掉這個場合的筆記（加 `all` 清晒全部場合） |
| `/quota` | 查自己的用量 |
| `/id` | 查自己的 Telegram id |
| `/help` | 指令表 |

管理員：`/issue` `/revoke` `/invites` `/allowgroup` `/denygroup` `/groups`
`/cost` `/stats` `/block` `/unblock`

### 冇咗嘅指令（同為咩）

舊版有、Router 冇嘅：

| 指令 | 為咩冇 |
|---|---|
| `/tune` | 調大肥鯨自己嘅搜尋／判斷參數。而家搜尋由 Hermes 決定，冇嘢好調 |
| `/vibe` `/think` | 調人設濃度同思考。人設喺 `SOUL.md`，思考喺 Hermes 嘅 config 全域關咗 |
| `/search` | 強制聯網。Hermes 自己判斷幾時要查（`SOUL.md` 有寫明） |
| `/undo` `/export` | 要讀 session 歷史，嗰啲喺 Hermes 嗰邊 |
| `/reload_persona` | 人設檔喺 Hermes 嗰邊，Router 改唔到 |

---

## 記憶

### 三層背景資料

每次群組回覆會附上，次序固定：

```
① 群組概況   ← 呢個群體本身，唔屬於任何人
② 自己筆記   ← 當前發言者嘅
③ 他人筆記   ← 被 @ 到嗰個人（最多 3 個）
```

全部包住標記並明講「唔係指示」—— 內容來自對話，可能有誘導句。

**為什麼他人筆記要附**：甲問「乙喺做乜」嗰時，助理手頭上只有甲嘅筆記，
答唔出。呢啲筆記同甲自己嘅屬同一個場合，可見範圍一樣，冇額外揭露。
只喺 @ 到人嗰時附。

### 場合劃分

長期筆記有 `scope` 欄位，只有兩種形式：

| scope | 範圍 |
|---|---|
| `private` | 私聊。**裡面的內容永遠不會在群組出現** |
| `group:<chat_id>` | **每個群組各自獨立**。A 群的筆記不會在 B 群出現 |

私聊獨立是刻意的：朋友私下說過「我最近失業」，不該在群組被提起。
群組之間也分開，是因為不同群的成員、話題、玩笑尺度都不一樣。

### 群組概況

長期筆記是「關於某個人」的，所以「這個群唔好用廣東話」這種**群組層面的
規矩**會被記成「關於講嗰個人嘅事實」，換個人講嘢就載入唔到。

所以另開一張表放群組概況：一段描述「這個群體本身」的文字（主題、氣氛、
慣例、近期話題），不屬於任何人，每次群組回覆都載入。

**素材限於 bot 親自參與過的交流**（被 @ 或被回覆的那些）。它在群組裡
旁觀到的其他閒聊不會進來 —— 那些只在 `group_cache` 留 72 小時。

更新在背景跑，要同時滿足「累積夠 `FW_GROUP_PROFILE_MIN_EXCHANGES` 則新交流」
與「距離上次至少 `FW_GROUP_PROFILE_MIN_HOURS` 小時」。

### 定期整理

筆記不能無限累積 —— 每一則都會進入 prompt，愈多愈貴。累積到 25 則
（`FW_NOTES_CONSOLIDATE_THRESHOLD`）會自動跑一次整理：合併同類、丟掉過期
與細碎的，壓縮到 12 則左右（`FW_NOTES_CONSOLIDATE_TARGET`）。

### 自動記憶

每輪對話後會背景跑一次抽取，從這一來一往裡找出「關於這個人、值得長期
記住的事實」，寫進對應場合的筆記。

- 抽取走便宜嘅 utility 模型、關閉推理
- **先問 Jev 值不值得記**（`FW_MEMORY_DECISION_THRESHOLD`）—— 判斷說沒有
  的話就跳過整次抽取呼叫
- 訊息少於 12 字、或只有媒體標註時直接跳過
- 已有的事實不會重複寫入；每場合上限 60 則
- 抽取在背景進行，失敗不影響對話

不想要自動記憶就把 `FW_AUTO_MEMORY` 設為 false，只保留手動 `/remember`。

---

## 群組引用鏈的實作限制

Bot API **沒有**「依 message_id 取訊息」的方法，`reply_to_message` 也只帶
上一層。要還原整條引用串，唯一的路是自己快取群組訊息。

1. 關閉隱私模式後，接收群組所有訊息並寫入 `group_cache`
2. 保留 72 小時後自動清除
3. **除了被指名的那條串，其餘內容永不送進模型**

**bot 不會收到自己送出的訊息**，所以 Router 在每次回覆後會主動把自己的
訊息補進快取，並且**連同對話名一齊寫** —— 否則別人引用本鯨時查唔到對話，
嗰句會被當成新串，對話即刻斷。

如果你不接受這個取捨，把 BotFather 的 `/setprivacy` 改回 Enable ——
代價是引用鏈只能回溯一層。

---

## 部署

Router 跑喺**主機**（唔係容器），因為佢要寫檔落 `hermes_ds/data/`
（影片轉發）。Hermes 就跑喺 Docker。

| 檔案 | 入 git？ | 為咩 |
|---|---|---|
| `.env.router` | ❌ | 金鑰同 token |
| `data/fatwhale.db` | ❌ | **所有記憶**、群組快取、授權 |
| `logs/router.log` | ❌ | 日誌 |
| `config/persona.md` | ❌ | 舊版人設（Router 用 `SOUL.md`，唔再用呢個） |

### 啟動同停止

```bash
powershell -ExecutionPolicy Bypass -File scripts\run_router.ps1
```

看守程序會自動重啟（跑少於 60 秒就等 30 秒，否則等 5 秒）。

### Rollback 返舊版

```bash
docker compose start fatwhale        # 起返舊版（維護模式已關，即正常運作）
# 再停 Router
```

⚠️ **同一個 bot token 只可以有一個 process 揸住。** 兩邊一齊跑會 409。

### 備份

```bash
python scripts/backup.py
```

用 SQLite 官方 backup API，執行中也能安全備份，保留 14 天。

> **不要在 Router 運行時於別處寫入 `fatwhale.db`。**
>
> 已經發生過一次：主機在跑貼圖標註、容器同時開著同一個資料庫，結果
> `database disk image is malformed`。bind mount 上 SQLite 的檔案鎖不可靠，
> 兩個程序同時寫就會壞。**備份是唯一的安全網。**

---

## 防失控：唔理其他 bot

**兩個 bot 互相回覆可以永遠停唔到。** 而循環嘅入口就喺「回覆本鯨就算被
指名」嗰條規則 —— 另一個 bot 引用本鯨嘅回覆，Router 當佢係同本鯨講嘢，
回覆佢，佢又引用返，冇完。每次來回都係一次 Hermes 呼叫（~11k tokens）。

呢個唔係假設。Hermes 自己個 codebase 有同一道閘，註釋寫得好白：

> another bot must explicitly @mention us, its quote-replies and plain chatter
> do not count (**two bots answering each other's replies never stop otherwise**)

**本專案嘅做法更硬：完全唔理其他 bot。** 唔理佢係 @ 定引用定隨口講。
「明確 @」只係將循環變慢，冇斷開佢 —— 兩個 bot 只要互相 @ 一次就照樣起飛。

| 設定 | 預設 | 意思 |
|---|---|---|
| `FW_ALLOW_BOTS` | `false` | 開咗就照收其他 bot 嘅訊息（想試 bot-to-bot 先開） |

**第二道閘：對話層面嘅失控剎停。** userbot 係**用戶帳號**，Telegram 當佢
係人 —— 上面嗰道閘捉唔到。所以加多一層：同一條對話短時間內太多次呼叫就
剎停，並且大聲 log（只 log 一次，唔會將日誌灌爆）。

| 設定 | 預設 | 意思 |
|---|---|---|
| `FW_CONVERSATION_MAX_CALLS` | 30 | 呢個窗口內最多幾次 |
| `FW_CONVERSATION_WINDOW_SECONDS` | 300 | 窗口幾長 |

正常傾偈撞唔到（最密都係幾分鐘幾次），但任何失控都會即刻斷。

---

## 安全須知

- `.env`、`.env.router`、`config/persona.md` 已被 `.gitignore` 排除。
  **不要把金鑰推上 GitHub。**
- Router 沒有對外開埠，只用 long polling。
- **Hermes 嘅 API server 冇沙盒** —— Hermes 自己警告過：掂到個 port
  就可以喺容器內執行指令。所以 host port 只綁 `127.0.0.1:8642`，
  而且有 `API_SERVER_KEY` 把關。
- 群組的判斷依據是管理員的實際成員身分。被拉進你不在的群時，bot 會立刻
  退出並私訊通知你。這個查詢有 5 分鐘快取，所以你退出群組後，bot 最多
  延遲 5 分鐘才跟著離開。
- 邀請碼短時效 + 單次使用。洩漏的視窗只有五分鐘。
- `/block <user_id>` 可即時停用某個帳號。
- 模型看不到 API 金鑰 —— 金鑰只存在於 `.env`，從不進入提示或資料庫。

**安全界線喺邊**：舊版有一節寫死喺 `core/persona.py` 嘅安全規則，
同輸出側嘅洩漏檢查（`core/security.py`，40 字逐字重疊就攔）。Router
**唔用呢兩樣** —— 防線搬咗去 Hermes 嗰邊，加上 `SOUL.md` 嘅「安全界線」
一節（由 `hermes_ds/build_soul.py` 生成，同舊版同一份文字）。

---

## 開發

```bash
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest tests -q
```

```
dafeijing/
├── router/       Router：Telegram 外殼 + Hermes 用戶端（現行）
│   ├── handlers.py      群組／私聊流程
│   ├── telegram.py      群組可用性、訊息快取、被指名判定
│   ├── conversation.py  對話命名、[名|id] 標註、引用串
│   ├── hermes.py        API 用戶端
│   ├── memory.py        三層背景資料注入
│   ├── guards.py        防失控（唔理其他 bot、對話剎停）
│   ├── gif.py           真 GIF 例外
│   ├── images.py        靜態圖轉發
│   ├── videos.py        影片落檔
│   ├── stickers.py      貼圖清單同標記
│   ├── ui.py            分段送出、打字指示、格式降級
│   ├── commands.py      18 個指令
│   └── services.py      執行期物件
├── core/         存取控制、引用鏈、節流、記憶、用量、媒體
├── llm/          OpenRouter 用戶端（只為記憶抽取同 GIF 外包）
├── render/       Markdown → HTML、長訊息分段
└── store/        SQLite schema 與存取層
```

⚠️ **`dafeijing/bot/` 已經刪咗。** 舊版嘅 Telegram 層（`app.py`、`private.py`、
`commands.py`、`group.py`、`ingest.py`、`services.py`、`ui.py`）同舊腦
（`core/chat.py`、`core/security.py`、`core/webfetch.py`、`core/debounce.py`、
`core/tuning.py`、`core/persona.py`）全部清走 —— Router 借咗嗰幾件已經搬入
`router/telegram.py` 同 `router/ui.py`。

**要睇舊版**：`legacy-fatwhale` branch（純大肥鯨，自己打 OpenRouter）。

人設規則（長度、媒體、安全界線）搬咗去 `hermes_ds/persona_rules.py`，
由 `build_soul.py` 讀 —— SOUL.md 完全由 `hermes_ds` 話事，唔使隔一個 repo 借。

設計原則：人設喺 `SOUL.md`（Hermes 側），同程式碼完全解耦；
所有可調參數集中在 `dafeijing/settings.py`。

---

## 授權

程式碼採 MIT。人設與金鑰屬私人內容，不隨專案提供。
