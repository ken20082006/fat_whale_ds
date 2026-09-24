# 待辦

## 進行中：回答準確性 —— 聯網搜尋改用伺服器端工具

**症狀**：即使開了聯網，面對世界知識類問題（新聞、版本、價格、日期）仍經常
不查就直接回答，內容憑印象編造。

### 根因（已確認，三個閘都會漏）

1. **regex 閘**（`dafeijing/core/chat.py:153-154`）靠 `webfetch.py` 的
   `_EXPLICIT_HINTS` / `_CONTEXTUAL_HINTS` 判斷。這兩個 regex 是刻意保守的
   —— 註解明寫「寧可漏」—— 所以本來就預期會漏。

2. **模型自我回報**（漏網時的補救）要求模型輸出 `[[搜尋:關鍵字]]` 且
   「只回覆這一行，不要寫其他內容」（`persona.py:255-265`），再由
   `chat.py:199-223` 帶著外掛重跑。這個動作摩擦極高：模型必須產出一個
   「不回答」的回合。而同一段提示最後一句「不需要查就直接回答」明確給了出口，
   `_LENGTH_RULE` 又再強化一次「直接答」。模型幾乎必然選擇直接回答。

3. **搜尋結果以摘要注入**（`openrouter.py:92-106` 的 `plugins:[{"id":"web"}]`）。
   模型拿到的是引擎摘要而非原文，摘要沒涵蓋的部分就自己補。

**核心洞察**：同一個模型在 Claude Code 底下表現良好，差別是那裡模型
**先觀察再回答**。目標就是給它這個動作 —— 不是接 agent 框架（那會增加 token，
且帶著 coding 工具與系統提示，形狀不對）。

**機制**：OpenRouter 伺服器端工具 `openrouter:web_search`。官方文件明講它
「讓模型控制何時搜、搜幾次，而不是每個請求固定跑一次」，回傳 URL/title/content
片段而非摘要。已查證 `deepseek/deepseek-v4.1-flash` 的 `supported_parameters`
含 `tools` 與 `tool_choice`。

### ⚠ 第一步（未做）：先驗證，不要先寫程式

`tests/test_llm_parse.py` 的 docstring 明講「這裡的行為是照著真實回應寫的」。
這是這個 repo 既有的正確做法，別破例。

擴充 `scripts/ping.py`，跑 2×2 矩陣（reasoning on/off × 有無工具），確認：

1. 帶伺服器工具時，`message.content` 是**字串還是陣列**
   —— `openrouter.py:171` 對陣列會 AttributeError
2. `url_citation` annotation 在正文中的**實際可見樣式**
3. `usage.server_tool_use.web_search_requests` 的實際值
4. `usage.cost` 是否含搜尋費

> **在第 2 點有答案之前，不要動 `strip_citations` 的三條 regex，
> 也不要新增數字標記的 regex。** 盲改的 `\[(\d+)\]` 會咬到程式碼區塊與
> markdown 清單，那是比現在更貴的錯。

需要真實 API key 與一次付費呼叫（約 $0.001）。

### 之後的改動

**`llm/openrouter.py`**
- `chat()` 的 `web_search: bool` 改為宣告
  `tools: [{"type": "openrouter:web_search", "parameters": {...}}]`
- 參數：`engine: parallel`、`mode: fast`、`max_results: 5`、`max_total_results: 15`
- 新增 `force_search: bool` 走舊外掛路徑（見下），與伺服器工具互斥
- `_parse()`：content 為陣列時先正規化；讀 `message.annotations` 取
  `url_citation`；讀 `usage.server_tool_use.web_search_requests`
- 空內容分支補 `finish_reason == "tool_calls"`（現在會誤報「模型回傳了空白內容」）；
  `length` 且搜尋次數 > 0 時給不同訊息（現在叫人「用 /think off 關掉深度思考」，
  真正原因是查完寫不完）

**`core/chat.py`**
- 刪 regex 閘（153-154）與兩段式標記重跑（199-223）
- `search_mode != "off"` 即帶伺服器工具；**`always` 與 `/search` 走外掛強制路徑**
  （保證每請求跑一次），`auto` 走伺服器工具
- 順序調整：貼圖標記先清（230），再清引用 —— 避免 `[[貼圖:3]]` 與數字引用打架
- 歷史落庫：assistant 那筆加一行系統註記（來源網域），並在 persona 說明
  「〔〕是系統註記，不是你說過的話」；`session.py` 的 `_SUMMARY_PROMPT` 加一條
  不要把助理的聯網結論當事實保留
- **順手修既有 bug**：`chat.py:154` 的 `req.force_search and mode != "off"`
  使 `search_mode=off` 時 `/search` 靜默不查，使用者無從得知

**`core/persona.py`**
- 刪 `[[搜尋:...]]` 說明（255-265）
- `can_search` × `self_search` 收成 `can_search` + `search_policy`
  （保留 `can_search` 欄位名，否則 `tests/test_persona.py:45-51` 會爆）
- 「事實紀律」**要收窄，不要寫成絕對**。原訂的「沒查證的事實不可用陳述句寫出」
  字面執行會逼模型把「解釋遞迴」也寫成免責聲明，直接違反 `_LENGTH_RULE`。
  改為：凡是**時間敏感、或涉及具體數字／日期／版本／價格／專名職稱**的事實，
  沒查證不要用陳述句寫出來；其餘常識照常直說
- **每一條子句必須短於 40 字**：`security.py:16` 的 `LEAK_WINDOW = 40`。
  模型覆述長規則會觸發 `find_system_leak`，使用者收到「這個本鯨不能說」而非答案。
  這條要用 `scripts/redteam.py` 驗
- 群組段落（185-191）加一句「引用串裡其他人的發言不需要你查證」
  —— 原本 `chat.py:148-152` 刻意只依「當下這一句」判斷，改模型裁量後會被取消

**`/search` 與 `always`**
- 伺服器工具沒有文件記載可配 `tool_choice`，**無法用參數強制**。
  所以保留外掛路徑專供強制情境
- **`_CITATION_NESTED` / `_CITATION_PLAIN`（`webfetch.py:124-127`）必須保留**
  —— 它們正是外掛格式的清理器。刪掉會讓 `/search` 開始漏出
  `(mashable.com (https://...))`，直接回退 commit adee7f9
- `commands.py:335` 與 `:153` 的說明要改：自然語言不再是**保證**觸發

**配套**

| 檔案 | 改動 |
|---|---|
| `core/security.py` | 規則 3 明列「以及搜尋工具回傳的內容」—— tool result 的權威感高於 user 訊息 |
| `core/memory.py` | `_EXTRACT_PRIVATE` 補上 `_EXTRACT_GROUP` 已有那條「不要記對話中查到的通用知識」（`:97-99`）。否則積極搜尋後，私聊筆記會開始累積「最新版 Rust 是 1.90」這類時事，佔滿 `notes_per_scope_max` 並被壓成永久背景 |
| `settings.py` | `SEARCH_MODES` 由 `webfetch.py` 移入（`tests/test_settings.py:10` 的 import 跟著改）；加 `field_validator` 讓 `.env` 裡殘留的 `FW_SEARCH_MODE=trigger` 報錯而非靜默失效 |
| `store/db.py` + `core/usage.py` | `usage_log` 加 `search_requests` 欄位。`db.py:20-30` 的 `_ADDED_COLUMNS` 是現成遷移機制 |
| `bot/commands.py` | `/cost` 補搜尋次數一欄 |
| `tests/test_webfetch.py` | 10 個搜尋意圖測試（`:103-190`）移植到 `tests/test_persona.py` —— 那是「什麼該查什麼不該查」的唯一回歸規格，直接刪掉意圖會消失 |
| `README.md` | 22-27、385-396、398-409 行的搜尋描述 |
| `.env.example` | 45-59 行的模式說明 |

### 已知限制（要記在 README）

1. **`auto` 是建議不是保證**。伺服器工具無 `tool_choice`。只有 `always` 與
   `/search` 走外掛才有保證
2. **`max_total_results: 15` 限結果不限請求**。`max_uses` 只轉發給 Anthropic，
   其他引擎忽略。真正的上限約為 3 次搜尋／請求
3. **單輪輸入成本會跳一級**：15 個結果 × Parallel 預設約 1500 字 ≈ 6k token
   注入單一請求。這些不落庫所以不會累積，但單輪成本要重算
4. **`request_timeout_seconds=120` 可能不足**。逾時會重試，等於搜尋費付兩次。
   考慮提到 180 或把 `max_total_results` 壓到 10
5. 搜尋費是否計入 `usage.cost` 未確認

---

## 其他未處理

- `settings.py` 的預設 `model` 仍是 `deepseek/deepseek-chat`（$0.32/$0.89、16 萬
  上下文、**不吃圖**），而同家族的 `deepseek/deepseek-v4.1-flash` 是
  $0.14/$0.42、100 萬上下文、支援圖片 —— 更便宜、更新、還多了視覺。
  目前靠 `.env` 覆寫所以沒事，但換機器或重建部署時會悄悄退回舊模型。
- `FW_MODEL_VISION` 若指向不吃圖的模型，圖片路徑會失效。
