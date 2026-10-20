"""集中設定。所有可調參數都在這裡，不散落在各模組。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 搜尋模式。放在這裡而不是 webfetch —— 設定值的合法範圍是設定層的事，
# 而且要在載入時就驗證，不要等到第一次搜尋才發現打錯字。
#   off      完全不搜
#   trigger  只認明講的（「上網查」「search 一下」）
#   auto     交給判斷（判斷已經交畀 Hermes —— 呢個值喺 Router 側淨低標籤用途）
#   always   每則都搜
SEARCH_MODES = ("off", "trigger", "auto", "always")

# 伺服器端工具 `max_results` 的容許上限。官方文件寫「1–25（Perplexity 係
# 1–20）」，這裡取**最低嗰個** —— engine 本身都係可調項，唔可以假設佢係邊個，
# 取最低就永遠唔會撞到上限。
#
# 超過的後果是全局而且嚴重的：**每一次**搜尋請求都會 HTTP 400
# （"Too big: expected number to be <=25"），等於整個搜尋功能廢掉。
# 實際撞過一次：`/tune set results_thorough 30`。
MAX_SEARCH_RESULTS = 20


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="FW_",
        extra="ignore",
    )

    # ── 必填 ──
    telegram_bot_token: str
    openrouter_api_key: str

    # ── OpenRouter ──
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_referer: str = "https://github.com/ken20082006/fat_whale_ds"
    openrouter_title: str = "Fat Whale DS"
    request_timeout_seconds: float = 120.0
    max_retries: int = 3

    # ── 模型 ──
    # 預設用 v4.1-flash 系而不是舊的 deepseek-chat：更便宜、上下文大（100 萬對 16 萬）。
    # 2026-10-19 由別名 `v4.1-flash` 換成釘死版本 `-1017`，
    # ⚠️ **貴 6.25 倍**（$0.30/$1.20 對 $0.048/$0.12）—— 換返平嘅係改返個名就得。
    #
    # ⚠️ Router 而家唔用呢個 model 答嘢 —— 對話交畀 Hermes。佢淨低嘅用途係
    # **用量紀錄嗰個標籤**（`router/handlers.py` 寫入 usage 表，`/cost` 靠佢）。
    # **真正答嘢嘅模型喺 `hermes/data/config.yaml` 嘅 `model:`，兩邊要一齊改。**
    #
    # 以前仲有個 `model_vision`：靜態圖要另揀一個吃得到圖嘅 model。而家
    # 圖片直接交畀 Hermes，冇人再讀嗰個設定 —— 已刪（2026-09-26）。
    model: str = "deepseek/deepseek-v4.1-flash-1017"
    model_utility: str = "deepseek/deepseek-v4.1-flash-1017"

    # 這個模型預設會做推理（reasoning），推理 token 以輸出計價且會佔用 max_tokens。
    # 實測關掉後單次費用約降為四分之一，對一般閒聊毫無損失。
    #
    # 2026-10-19：以前仲寫住「使用者可用 /think 個別開啟，唔指定時由決策模型
    # 逐則判斷」—— **兩邊都已經唔存在**。`/think` 冇搬過嚟（見 router/commands.py
    # 檔頭），Jev 亦已刪。而家推理係恆常關掉，冇任何 runtime 開關。
    # 想開就改呢個值再重啟（記住 Hermes 嗰邊亦要跟 —— 回答用 Hermes 個腦）。
    reasoning_enabled: bool = False

    # ── 決策模型（Jev）已刪（2026-10-19）────────────────
    # 曾經有三個用途，全部隨 Hermes 遷移而死剩一個記憶閘，而嗰個閘又壞咗：
    # TypeSafe 個端點由 2026-10-07 起對我哋嘅地區回 403
    # （"not available in your region"，endpoint_count: 1，冇 fallback），
    # 30 次呼叫 0 次成功。因為用戶端失敗時一律回 None、而呼叫端收到 None
    # 就當「值得記」放行，所以個閘實際上冇閘過任何嘢，只係每則訊息白花一次呼叫。
    #
    # 記憶閘而家淨靠 `core/memory.py` 嗰道**免費**嘅啟發式閘（`_worth_extracting`）。
    # 代價講明：嗰道閘只擋短訊息／純媒體標註，擋唔到「長但冇內容」嘅閒聊，
    # 嗰種會多跑一次 utility 抽取。詳見 ARCHITECTURE.md。
    #
    # 連帶刪走嘅設定（全部零 caller）：decision_enabled、decision_model、
    # decision_endpoint、decision_timeout_seconds、decision_min_confidence、
    # decision_context_messages、reasoning_budget_low、reasoning_budget_high、
    # memory_decision_threshold。
    # ── 群組聊天室模式 ──────────────────────────────────
    # 兩個開關本身係 **per-group、存 DB**（見 core/room.py），唔喺呢度。
    # 呢兩條係窗口大細 —— 全局共用。
    #
    # 每次被 @ 送「cursor 之後嘅新訊息」，最多咁多則；爆就只留最新嗰批，
    # 前面加一句標記講明漏咗幾多。從未送過（冇 cursor）就補最近咁多則。
    # 實測：30 則大約等於一輪閒聊，足夠睇得出「講緊乜」。
    group_room_window_messages: int = 30
    # token 保險閘。窗口太長就交 `ReplyChain._trim()` 摺疊中段
    # （同引用串共用嗰個機制）。
    group_room_window_tokens: int = 6_000

    # ── 聯網 ──
    # 搜尋積極程度：
    #   off      完全不搜
    #   trigger  只認明講的（「上網查」「search 一下」）
    #   auto     加上情境判斷（時間敏感、版本、價格之類）
    #   always   每則都搜（最準但也最慢，且會搜「你好」這種）
    search_mode: str = "auto"
    # 實測同一題的費用：exa $0.0083、parallel $0.0057、parallel+turbo $0.0018、
    # parallel+fast $0.0013。預設選最便宜的組合，品質實測沒有明顯差異。
    # exa 是 DeepSeek 這類非原生模型的官方預設，若覺得品質不穩可以改回去。
    search_engine: str = "parallel"
    # 引擎自身的分級，與上面的 search_mode 是兩回事 ——
    # 前者決定「引擎怎麼搜」，後者決定「什麼時候搜」。
    # fast 對 exa 與 parallel 都有效；留空則用引擎預設（較貴）。
    search_engine_mode: str = "fast"
    search_max_results: int = 5
    # 判斷說「一個明確事實就夠」時撈幾條；說「小眾冷門、撈少會漏」時撈幾條。
    # 中間值就是上面的 search_max_results。
    #
    # 撈幾多條是唯一能調召回率的旋鈕 —— 外掛每次請求只搜一次，查詢由引擎
    # 自己從對話推導，模型無權指定。小眾名詞（例如某個型號名是否存在）
    # 撈得少就會直接漏掉。
    # 上下限見 MAX_SEARCH_RESULTS。這幾個數字**改錯會令每次搜尋都失敗**，
    # 所以載入時就驗證，不要等到第一次搜尋才發現。
    search_results_quick: int = 3
    search_results_thorough: int = 10
    # 伺服器端工具一次請求的結果總上限。**限結果不限請求** —— 這個參數只
    # 轉發給部分引擎，其餘會忽略，真正的搜尋次數上限約 3 次。
    search_max_total_results: int = 15
    # 一則訊息最多讀幾個對方貼的連結
    fetch_max_urls: int = 3

    # 告訴模型「今天」是哪一天。用它才判斷得出「最新」是相對什麼時候。
    # 香港沒有日光節約，固定位移即可，不需要 tzdata。
    timezone_offset_hours: int = 8
    timezone_label: str = "香港時間"

    # ── 思考過程（beta）──────────────────────────────────
    # 把模型的思考過程貼出來，收在一個「撳一下展開」的引用塊裡面。
    #
    # **beta，預設關著。** 關著的時候完全不會出現，原有行為一模一樣。
    # 開啟之後仍然只有**推理真的開過**的回覆才有內容可貼 —— 推理沒開就
    # 沒有思考過程。
    #
    # ⚠️ 2026-10-19：呢段以前寫住「見 /think，以及 Jev 的逐則判斷」——
    # **兩者都已死**（`/think` 冇搬過嚟，Jev 已刪，見上面）。而家推理係
    # 恆常關掉（`reasoning_enabled`），所以呢個功能開咗都大機會冇嘢貼。
    #
    # 思考內容是模型自言自語的原文，可能覆述到人設或系統提示，所以貼之前
    # 一定過一次 find_system_leak，命中就**整段不貼**。它也不會落庫 ——
    # 落庫的話下一輪歷史會多一段，模型就會當成自己講過的話。
    #
    # 開啟方式：/tune set why on（可隨時關掉，即時生效）
    show_reasoning: bool = False
    # 貼出來的長度上限。思考可以極長，不設界會洗版。
    show_reasoning_max_chars: int = 1500

    # ── 維護模式 ──
    # 遷移或改版期間用：群組被指名也不做任何真工作（不叫模型、不查引用串、
    # 不看片、不抽記憶），只回一句人設語氣的「調整緊」。
    #
    # 為什麼要這個：改版期間群組是 live 的，但答案會唔準。與其靜靜哋答錯，
    # 不如老實講調整緊。**私聊完全不受影響** —— 只影響群組的回應路徑。
    #
    # 開啟方式：/tune set maintenance on（即時生效，不必重啟容器）
    maintenance_mode: bool = False
    # 同一個群在這段時間內只出一則通知。通知不是答案，整個群每個人 @ 一次
    # 就會洗版。純記憶體，重啟歸零。
    maintenance_notice_cooldown_seconds: int = 600

    # ── Router（大肥鯨外殼 + DeepSeek Harness 腦）────────
    # dsh 嘅協議見 `router/dsh.py` 檔頭；架構背景見
    # hermes/ARCHITECTURE.md（嗰份主要寫 Hermes 年代）。
    #
    # ⚠️ **用 stdio SDK，唔係 HTTP。** dsh 個 web HTTP API 有 process-scoped
    # 認證：launchToken 每條 process 隨機生成、cookie 綁 in-memory peer，
    # 而且 `Config` 冇任何開關可以關掉（2026-10-20 讀源碼確認）。
    # SDK profile 冇呢個問題 —— 信任邊界就係 process 本身。
    #
    # Router 跑喺容器、dsh 都跑喺容器，所以呢度直接叫 **entrypoint**，
    # 唔係 `dsh` binary —— entrypoint 負責把 SOUL.md 砌入 persona
    # （`dsh/data` 係 bind mount，會蓋走 build 時嘅 profile，每次開機要重砌）。
    dsh_command: str = "/usr/local/bin/dsh-entrypoint"
    # 一輪等幾久。dsh 會做壓縮、可能重試，同 Hermes 年代一樣畀 180 秒。
    dsh_timeout_seconds: float = 180.0
    # 起 process 要載入成個 Cordis plugin tree。
    dsh_startup_timeout_seconds: float = 90.0

    # ⚠️ 下面兩個係 Hermes 年代留落嘅，**而家冇 caller**（dsh 收 base64
    # 圖片，唔收檔案路徑）。留住係因為 `router/videos.py` 仲寫緊嗰個目錄，
    # 他日要將影片送得入 dsh 會再用到。
    hermes_media_dir: Path = Path("hermes/data/cache/videos")
    hermes_media_prefix: str = "/opt/data/cache/videos"

    # 影片轉發用。Hermes 嘅 `video_analyze` 要一個**佢讀得到嘅路徑**，
    # 唔收 data URL（`input_file` 會 400），所以 Router 要落一個檔。
    #
    # Router 跑喺主機、Hermes 跑喺容器，同一個檔有兩個路徑：
    #   hermes_media_dir      —— 主機寫入（相對於 fat_whale_ds 嘅 CWD）
    #   hermes_media_prefix   —— 容器讀取（喺提示度話畀模型知）
    #
    # 預設值啱啱好：hermes 掛咗 `./data:/opt/data`，所以喺 data 底下
    # 寫就兩邊都見到。改咗 Hermes 嘅掛載就要跟住改。
    hermes_media_dir: Path = Path("hermes/data/cache/videos")
    hermes_media_prefix: str = "/opt/data/cache/videos"

    # 成本估算（美元／百萬 token）。
    #
    # ⚠️ Hermes 嘅 API **只回 tokens，唔回 cost**，所以 Router 要自己估。
    # 呢兩個數係 OpenRouter `/v1/models` 嘅實價。2026-10-19 換模型時已核對：
    # `v4.1-flash-1017` 嘅預設 route（$0.30/$1.20）啱好同舊值一樣，所以呢兩個
    # 數唔使改。**但呢個係彩數** —— 下次換模型要重新查，唔可以假設。
    #
    # ⚠️ OpenRouter 每個 provider 收費唔同（同一個 model id 可以有十幾個
    # endpoint，$0.05 到 $0.30 都有），呢兩個數係**預設 route** 嗰個。
    #
    # 換模型就要跟住改 —— 唔改嘅話 `/cost` 會報錯數。準確數字始終喺
    # Hermes 自己個 `state.db`（`session_model_usage.estimated_cost_usd`），
    # 但讀佢內部 DB 太脆弱，所以唔做。
    hermes_input_price: float = 0.30
    hermes_output_price: float = 1.20

    # ── 防失控 ──────────────────────────────────────────
    # **完全唔理其他 bot 發嘅訊息。** 兩個 bot 互相回覆可以永遠停唔到 ——
    # Hermes 自己個 codebase 都有同一道閘，註釋寫明
    # 「two bots answering each other's replies never stop otherwise」。
    #
    # 唔跟 Hermes 嗰套（允許明確 @）係因為「明確 @」只係將循環變慢，
    # 冇斷開佢。群組裡面正常唔會需要同另一個 bot 傾偈。
    allow_bots: bool = False

    # userbot 係用戶帳號，上面嗰道閘捉唔到，所以加多一層：
    # 同一條對話喺呢個窗口內超過咁多次呼叫就剎停。正常傾偈撞唔到。
    conversation_max_calls: int = 30
    conversation_window_seconds: float = 300.0

    # ── 身份 ──
    # Router 自己講自己嗰陣用嘅名：/help、/context、/new、邀請碼、出錯訊息。
    #
    # **角色本身唔喺呢度。** 人設喺 Hermes 嗰邊嘅 SOUL.md，呢個只係
    # Router 出嘅字。Router 係兩隻 bot 共用嘅代碼，唔應該綁死其中一隻嘅角色
    # —— 所以由設定帶入，預設值就係大肥鯨原本寫死嗰個，唔填行為完全一樣。
    self_name: str = "本鯨"

    # ── 路徑 ──
    persona_file: Path = Path("config/persona.md")
    db_path: Path = Path("data/fatwhale.db")
    log_dir: Path = Path("logs")

    # ── 授權 ──
    admin_user_ids: str = ""
    invite_ttl_seconds: int = 300  # 邀請碼有效期，預設五分鐘

    # ── 長期記憶 ──
    # 自動從對話中抽取關於使用者的事實，使用者不必手動 /remember
    auto_memory: bool = True
    # 單一場合的筆記上限（硬性backstop），超過就丟最舊的
    notes_per_scope_max: int = 60
    # 累積到這個數量就觸發整理，把零碎筆記合併壓縮
    notes_consolidate_threshold: int = 25
    # 整理後希望留下的則數
    notes_consolidate_target: int = 12

    # ── 群組概況 ──────────────────────────────────────────
    # 每個群一則「這個群體本身」的概況（主題、氣氛、慣例），見
    # core/groupprofile.py。與 memory_notes 不同，它不屬於任何一個人。
    #
    # 素材限於機器人親自參與過的交流（被 @ 或被回覆的那些），所以累積得慢，
    # 門檻不必高。
    group_profile_enabled: bool = True
    group_profile_min_exchanges: int = 8
    group_profile_max_exchanges: int = 60
    # 太頻繁更新只會讓概況跳來跳去，而且每次都多付一次模型呼叫。
    group_profile_min_hours: float = 6.0
    # 長度上限。刻意短 —— 它每次群組回覆都會進 system prompt。
    group_profile_max_chars: int = 600

    # ── Session ──
    # 模型有 1M 上下文，且輸入每百萬 token 僅 $0.1，所以視窗可以開得比一般保守值大。
    # 30 輪約一萬多 token，每個請求的輸入成本仍在千分之一美元量級。
    window_turns: int = 30
    compact_trigger_tokens: int = 12_000
    summary_max_tokens: int = 800
    idle_reset_minutes: int = 480
    # max_tokens 是上限而非預留，但設得太寬模型就會寫滿，回覆變成洗版。
    # 這是防失控的保險，真正的長度控制寫在提示裡（persona 的「長度」那一節）。
    private_reply_max_tokens: int = 2400
    group_reply_max_tokens: int = 1100
    # 「管理員在不在這個群組」的快取時間。Telegram 對 getChatMember 有速率限制，
    # 而每則群組訊息都要判斷一次，所以必須快取。
    group_membership_ttl_seconds: int = 300
    group_chain_max_messages: int = 40
    group_chain_max_tokens: int = 12_000
    group_thread_ttl_minutes: int = 720

    # ── 節流 ──
    debounce_seconds: float = 1.5
    rate_per_minute: int = 20

    # ── 保存期限 ──
    history_retention_days: int = 30
    group_cache_retention_hours: int = 72

    # ── 媒體 ──
    # 縮圖的長邊上限。只在原圖比它大時才縮，不會放大。
    #
    # 1568 而不是 1024：Telegram 給的照片最大通常約 1280 邊，
    # 設 1024 等於每一張都被縮一次；設 1568 則一般不觸發縮圖，送的是原樣。
    # 代價是 image token 約為像素數 ÷ 750 —— 1280×960 由約 1 050 升至約 1 640。
    # 貼圖與影片的縮圖本身就更小，調高這個值幫不到它們（見 media.pick_file）。
    image_max_edge: int = 1568
    # 單一媒體的下載上限，量的是實際下載的位元組數，不是原始素材的大小 ——
    # 靜態圖片與貼圖縮圖都遠低於這個值。真正受限的是 GIF 本體與要抽格的影片。
    image_max_bytes: int = 8 * 1024 * 1024
    # 影片與動圖沒有抽格設定 —— 一律外包，見下面的 video_delegate_*。

    # ── 影片與動圖 ────────────────────────────────────────
    #
    # **Router 唔再打任何媒體 model**（2026-09-26 起）。影片同真 GIF 一律
    # 落檔交畀 Hermes 嘅 `video_analyze`，真 GIF 先轉做 MP4。
    #
    # 點解要轉：Hermes 嘅 `_VIDEO_MIME_TYPES` 冇 `.gif`。而原本交外援模型
    # 嗰條路實測**唔可信** —— 一條「藍方塊固定、紅圓水平向右移」嘅測試 GIF，
    # `xiaomi/mimo-v2.6-flash` 答成「兩個都係垂直移動」；同一個模型收到轉
    # 出嚟嘅 MP4 就答得完全正確，而且快五倍。
    #
    # 所以下面兩個上限淨低嘅用途係「決定邊啲檔唔落」—— 喺轉檔之前先篩。

    # 下載上限，量嘅係實際位元組數。超過就當冇呢個媒體。
    video_delegate_max_bytes: int = 10 * 1024 * 1024

    # 長度上限。影片輸入按秒計費，一條 5 分鐘片唔值得默默做。超過就當冇
    # —— 同大肥鯨一致：睇唔到就直接唔睇，唔會求其抽格充數。
    #
    # 注意呢個通常比大小上限**更遲**觸發：5 分鐘 720p 通常遠超 10MB，
    # 所以「太長」同「太大」要分開判斷。
    video_delegate_max_seconds: float = 300.0


    @property
    def admin_ids(self) -> set[int]:
        out: set[int] = set()
        for chunk in self.admin_user_ids.replace(";", ",").split(","):
            chunk = chunk.strip()
            if chunk.isdigit():
                out.add(int(chunk))
        return out

    def is_admin(self, tg_user_id: int | None) -> bool:
        return tg_user_id is not None and tg_user_id in self.admin_ids

    @field_validator(
        "search_results_quick", "search_max_results", "search_results_thorough"
    )
    @classmethod
    def _validate_search_result_counts(cls, value: int, info) -> int:
        """撈幾多條有硬上限，設錯會令每一次搜尋都 400。

        這一道守住 `.env`；`/tune` 那條路是另一道閘（見 core/tuning.py 的
        Knob.minimum / maximum）—— 兩條路都要守，因為兩者都可以改壞。
        """
        if not 1 <= value <= MAX_SEARCH_RESULTS:
            raise ValueError(
                f"{info.field_name} 要在 1–{MAX_SEARCH_RESULTS} 之間，收到 {value}"
            )
        return value

    @field_validator("search_max_total_results")
    @classmethod
    def _validate_total_results(cls, value: int) -> int:
        """總上限只設下限 —— 官網冇寫上限（未指定時預設 50）。"""
        if value < 1:
            raise ValueError(f"search_max_total_results 最少 1，收到 {value}")
        return value

    @field_validator("search_mode")
    @classmethod
    def _validate_search_mode(cls, value: str) -> str:
        """打錯字要立刻報錯，不要靜默失效。

        未知的模式會落到 wants_search() 的預設分支，行為像 auto ——
        設定的人以為生效了，其實沒有。
        """
        if value not in SEARCH_MODES:
            raise ValueError(
                f"FW_SEARCH_MODE 只能是 {' / '.join(SEARCH_MODES)}，收到 {value!r}"
            )
        return value

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
