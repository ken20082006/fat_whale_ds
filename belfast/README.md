# belfast/ —— 貝爾法斯特

第二個人設（《碧藍航線》皇家女僕隊女僕長）。bot：**@Grok_KKSK_bot**（顯示名「女僕長」）。

**同一份代碼、同一個 Router、同一個架構 —— 只係換人設同 model。**
呢個目錄就係 `hermes/` 嘅對應物，結構刻意一模一樣。

```
Telegram → Router（dafeijing/router）→ Hermes API → 回覆
              ↑ 兩隻 bot 各一個 instance      ↑ 各一個 container
```

| | `hermes/`（大肥鯨）| `belfast/` |
|---|---|---|
| 人設本體 | `../config/persona.md` | **`persona.md`** |
| 核心 | 愛認叻但底子虛，一追問就露底 | 可靠、從容、帶一點玩味 |
| 自稱 / 稱呼 | 「本鯨」/「你」 | 「我」/「指揮官」 |
| Model | OpenRouter / `deepseek-v4.1-flash` | **xAI / `grok-4.7`** |
| Telegram 顯示名 | 大肥鯨 | 女僕長 |
| Container | `hermes` | **`belfast`** |
| Port | 8642 | **8643**（主機）|
| DB / log | `data/fatwhale.db` / `logs/` | **`belfast/data/router.db` / `belfast/logs/`** |

**共通嘅（唔應該改）**：Router 代碼、`FW_SELF_NAME` 以外嘅設定、搜尋／記憶／
長度規則、**安全界線**。呢啲係「系統點運作」，唔係角色設定。

---

## `data/SOUL.md` 係生成嘅

```
persona.md（手寫，角色本體）
  + ../hermes/persona_rules.py 嘅三節（長度／媒體／安全界線）
  + build_soul.py 入面嘅 Hermes framing（6 節）
  = data/SOUL.md
```

```bash
cd C:\ds\fat_whale_ds
.venv/Scripts/python belfast/build_soul.py
```

⚠️ **唔好手改 `data/SOUL.md`** —— 下次重跑就唔見咗。

規則照樣 **import 大肥鯨嗰份**（`../hermes/persona_rules.py`），**唔複製** ——
`_SECURITY_RULE` 係反 prompt injection 嘅唯一防線，兩隻 bot 必須逐字一致。
實測長度 6,357 字元。

`build_soul.py` 只換走兩處大肥鯨專屬嘅字：
`MEDIA_BRIDGE` 嘅「本鯨睇唔到」→「我睇唔到」，同 `_MEDIA_RULE` 尾段嗰句
「偷偷外包給別家模型」自嘲。`BACKEND_RULE` 就寫明係貝爾法斯特。
**其餘一字不動**，而且上游改咗句嘢會**報錯停低**而唔係靜靜哋出貨。

---

## 點起

⚠️ **呢個目錄冇 `docker-compose.yml`。** 成套系統（兩個腦 + 兩個 Router）
收埋喺 **repo 根** 嗰個 compose —— 由 2026-09-26 起。

```bash
cd C:\ds\fat_whale_ds          # ← repo 根，唔係 belfast/
docker compose up -d           # 四個 service 一齊起
docker compose logs -f router-belfast
```

見到 `Router 上線：@Grok_KKSK_bot｜Hermes http://belfast:8642` 就成功。

| service | 角色 | 連去邊 |
|---|---|---|
| `hermes` | 大肥鯨個腦 | — |
| `router` | 大肥鯨個外殼 | `http://hermes:8642` |
| `belfast` | 貝爾法斯特個腦 | — |
| `router-belfast` | 貝爾法斯特個外殼 | `http://belfast:8642` |

改咗 `dafeijing/` 之後要重建 Router 個 image：

```bash
docker compose up -d --build router router-belfast
```

### 首次設定

```bash
# 1. Hermes 生成自己條 key（第一次起之後）
grep API_SERVER_KEY belfast/data/.env

# 2. 填入 belfast/.env.router 嘅 FW_HERMES_KEY，再重啟
docker compose restart router-belfast
```

### 兩個「搬入 Docker 之後一定要知」嘅坑

**一、`FW_HERMES_URL` 一定要改。** 容器入面 `127.0.0.1` 係自己，連唔到 Hermes。
compose 用 `environment:` 蓋成 `http://belfast:8642` —— 唔好刪嗰行。

**二、Router 容器要行 root。** Hermes 用 uid 10000 建立 `data/`，
而 `belfast/data` 係 **700** —— Router（image 預設 uid 1000）連 DB 都開唔到
（實測：`sqlite3.OperationalError: unable to open database file`）。
`docker-compose.yml` 嗰個 `user: "0:0"` 就係為咗呢樣。

### 唔可以撞嘅三樣

| | 撞咗會點 |
|---|---|
| `FW_TELEGRAM_BOT_TOKEN` | 同大肥鯨一齊 polling → 409，Hermes 會主動踢走對手 |
| `FW_DB_PATH` | 兩個 process 寫同一個 SQLite → `database disk image is malformed`（README 記載過呢次事故）|
| `FW_HERMES_MEDIA_DIR` | `router/videos.py` 有個 6 小時 pruner，會**刪晒個目錄所有舊檔** → 兩隻 bot 互刪對方啱啱寫嘅片 |

---

## 已知差異（唔係 bug，係取捨）

- **貴過大肥鯨好多。** deepseek `$0.14/$0.42` vs grok-4.7 `$2.00/$6.00`
  —— 輸入同輸出都係 **14×**。想要嘅 `grok-4-fast` 唔喺呢條 key 嘅可用清單。
  grok-4.7 係 500K context（grok-4.3 有 1M），超過 200K 之後輸入再倍上 $4.00。
- **思考關唔到。** 大肥鯨行 deepseek，`reasoning_effort: "none"` 真係關得掉。
  xAI 呢條 route 只收 `low/medium/high`，傳 `"none"` 會被丟棄 ——
  所以設定係 `"low"`。即係貝爾法斯特會諗少少嘢，大肥鯨唔會。
- **Jev 決策閘關咗**（`FW_DECISION_ENABLED=false`）—— 嗰個端點係 OpenRouter 獨有，
  而呢隻 bot 全行 xAI，唔想食大肥鯨剩低嗰 $2.75。後果：記憶抽取冇前置閘。
- **GIF 描述改用 grok**（`FW_GIF_DELEGATE_MODEL`）。大肥鯨實測揀 xiaomi 係因為
  「真 GIF 只有佢睇得到」—— grok 收唔收真 GIF **未實測**。睇唔到係靜默失敗。

## 未做

- 冇 `git init` 以外嘅嘢 —— 呢個 dir 跟 repo，唔使另開 repo。
- **未實測**：grok 睇真 GIF、睇片；`reasoning_effort: "low"` 實際有冇減到思考。
