# 大肥鯨

DeepSeek 的擬人化 Telegram Bot。私人邀請制，只有拿到邀請碼的人能用。

不是公開服務，也不是框架 —— 它是一隻養在自家 VPS 上的鯨魚。

---

## 特色

**對話**

- 三層記憶：短期原文視窗、中期滾動摘要、長期跨對話筆記
- 閒置自動重置，但摘要延續，不會「忽然失憶」
- 訊息自動合併：連續送出的一串訊息會合成一輪，省 token 也更連貫
- 回覆以 Telegram HTML 輸出，長文自動分段且不切斷程式碼區塊
- `/vibe` 調整人設濃度，`/context` 檢視目前記憶佔用

**群組**

- 只有被 @ 或被回覆時才回應，其餘訊息不讀不答
- 完整引用鏈回溯：引用再引用也追得到源頭
- **只有管理員能把本 bot 加入群組**，其他人加的一律立刻退出並通知你
- 群組以「串」為單位記憶，天然有界，不會無限累積

**授權**

- 邀請碼一次性、預設五分鐘失效，用 Telegram 深連結一鍵綁定
- 群組白名單，未授權群組自動退出

**成本**

- 完整記錄每人每次的 token 與費用
- 預設只記錄不封鎖，跑一段時間後再決定配額

---

## 快速開始

### 1. 建立 bot

在 Telegram 找 [@BotFather](https://t.me/BotFather)：

```
/newbot                          # 建立 bot，取得 token
/setprivacy                      # 選 Disable —— 必須關閉，否則群組引用鏈無法回溯
```

> `/setjoingroups` 可以完全禁止任何人把 bot 加入群組，但連你自己也加不了。
> 本專案改用程式判斷「是誰加的」，因此**不需要**動這個設定。

### 2. 取得你的 user id

對 [@userinfobot](https://t.me/userinfobot) 說話，或先啟動 bot 後用 `/id`。

### 3. 安裝

```bash
git clone https://github.com/ken20082006/fat_whale_ds.git
cd fat_whale_ds

py -3.12 -m venv .venv                 # Windows
# python3.12 -m venv .venv             # Linux

.venv/Scripts/python -m pip install -r requirements.txt   # Windows
# .venv/bin/pip install -r requirements.txt               # Linux
```

### 4. 設定

```bash
cp .env.example .env
cp config/persona.example.md config/persona.md
```

編輯 `.env`，至少填入 `FW_TELEGRAM_BOT_TOKEN`、`FW_OPENROUTER_API_KEY`、`FW_ADMIN_USER_IDS`。
再編輯 `config/persona.md` 寫你的角色設定。

### 5. 啟動

```bash
.venv/Scripts/python -m dafeijing.main
```

看到「大肥鯨上線」就成功了。在 Telegram 對它說 `/id` 確認身份，再用 `/issue` 產生第一張邀請碼。

---

## 指令

| 指令 | 說明 |
|---|---|
| `/new` | 開新對話，保留摘要（軟重置） |
| `/reset` | 清空這段對話的記憶，長期筆記保留 |
| `/undo` | 收回上一輪 |
| `/context` | 看看目前記得多少 |
| `/export` | 匯出對話為 Markdown |
| `/vibe low\|mid\|high` | 調整人設濃度 |
| `/remember <內容>` | 寫入長期記憶 |
| `/forget` | 清空長期記憶 |
| `/quota` | 查自己的用量 |
| `/id` | 查自己的 Telegram id |

管理員：`/issue` `/revoke` `/invites` `/allowgroup` `/denygroup` `/groups` `/cost` `/stats` `/block` `/unblock` `/reload_persona`

---

## Session 設計

| 層 | 內容 | 生命週期 |
|---|---|---|
| 短期 | 最近 12 輪原文 | 滑動視窗 |
| 中期 | 滾動摘要，上限 400 token | session 內延續 |
| 長期 | 筆記，跨對話永久 | 直到 `/forget` |

每次送出的內容順序固定：`人設 + 對象 + 長期筆記 + 摘要 → 最近原文 → 新訊息`。

**壓縮**：視窗超過 12 輪或 3000 token 時，把最舊的一半併入摘要。
**閒置重置**：私聊 120 分鐘、群組串 6 小時無活動即軟重置 —— 摘要留著，原文清掉。

關鍵取捨：軟重置而非硬重置。使用者感覺得到連貫性，成本卻回到最低。

所有參數都在 `.env` 可調，見 `.env.example`。

---

## 群組引用鏈的實作限制

Bot API **沒有**「依 message_id 取訊息」的方法，`reply_to_message` 也只帶上一層。
要還原整條引用串，唯一的路是自己快取群組訊息。

因此本專案會：

1. 關閉隱私模式後，接收群組所有訊息並寫入 `group_cache`
2. 保留 72 小時後自動清除
3. **除了被指名的那條串，其餘內容永不送進模型**

另外，bot 不會收到自己送出的訊息，所以本專案在每次回覆後會主動把自己的訊息補進快取，
否則別人回覆 bot 時引用鏈會斷在該處。

如果你不接受這個取捨，把 BotFather 的 `/setprivacy` 改回 Enable，並停用 `group_cache`
相關邏輯即可 —— 代價是引用鏈只能回溯一層。

---

## 部署

### Docker（推薦）

```bash
docker compose up -d --build
docker compose logs -f
```

`docker-compose.yml` 已把 `data/`、`logs/`、`config/persona.md` 掛載出來，容器重建不會遺失。

### systemd

見 `deploy/fatwhale.service`，安裝步驟寫在檔案開頭。

### 備份

```bash
python scripts/backup.py
```

用 SQLite 官方 backup API，執行中也能安全備份，保留 14 天。

---

## 安全須知

- `.env` 與 `config/persona.md` 已被 `.gitignore` 排除。**不要把金鑰推上 GitHub。**
- bot 沒有對外開埠，只用 long polling，不需要設定防火牆。
- 群組採白名單制。非管理員把 bot 加入群組時，bot 會立刻退出並私訊通知你。
- 邀請碼短時效 + 單次使用。洩漏的視窗只有五分鐘。
- `/block <user_id>` 可即時停用某個帳號。

---

## 開發

```bash
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest tests -q
```

```
dafeijing/
├── bot/          Telegram handler：指令、私聊、群組、輸出
├── core/         存取控制、session、引用鏈、節流、人設、用量
├── llm/          OpenRouter 用戶端
├── render/       Markdown → HTML、長訊息分段
└── store/        SQLite schema 與存取層
```

設計原則：`config/persona.md` 與程式碼完全解耦，改人設不必動程式；
所有可調參數集中在 `dafeijing/settings.py`，不散落各處。

---

## 授權

程式碼採 MIT。人設與金鑰屬私人內容，不隨專案提供。
