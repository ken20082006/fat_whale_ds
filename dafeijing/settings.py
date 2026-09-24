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
#   auto     交給判斷（見 llm/decisions.py）
#   always   每則都搜
SEARCH_MODES = ("off", "trigger", "auto", "always")


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
    # 預設用 v4.1-flash 而不是舊的 deepseek-chat：更便宜（$0.14/$0.42 對
    # $0.32/$0.89）、上下文大 6 倍（100 萬對 16 萬）、而且**支援圖片**。
    # 舊預設不吃圖，`.env` 一旦遺失或換機重建，視覺能力會靜靜失效。
    model: str = "deepseek/deepseek-v4.1-flash"
    model_vision: str = "deepseek/deepseek-v4.1-flash"
    model_utility: str = "deepseek/deepseek-v4.1-flash"

    # 這個模型預設會做推理（reasoning），推理 token 以輸出計價且會佔用 max_tokens。
    # 實測關掉後單次費用約降為四分之一，對一般閒聊毫無損失。
    # 使用者可用 /think 個別開啟。
    #
    # 這個值是**降級用的後備**：使用者沒指定 /think 時由決策模型逐則判斷，
    # 決策模型不可用時才退回這裡。見 llm/decisions.py。
    reasoning_enabled: bool = False

    # ── 決策模型（Jev）──
    # TypeSafe 的 Jev，走獨立端點，不是聊天模型。用來在呼叫主模型前判斷
    # 這一則需不需要深度思考。$0.042/M 輸入、輸出免費，大約 $0.00002/次。
    decision_enabled: bool = True
    decision_model: str = "typesafe/jev-1.13"
    decision_endpoint: str = "https://openrouter.ai/api/alpha/decisions"
    # 它擋在主回覆前面，逾時要短 —— 等太久不如直接降級。
    decision_timeout_seconds: float = 8.0
    # 判斷附帶的信心值（0–1）。低於這個值就當作「沒判斷」，退回後備 ——
    # 分布很平的時候（實測見過 0.23）不該硬選一個。
    decision_min_confidence: float = 0.35
    # 判斷說要思考時，max_tokens 放寬幾倍。推理 token 會**吃掉**這個額度，
    # 用完 content 會變 null（使用者收到「本鯨想得太久，額度用完了」）。
    # 思考越深就多留一點；不說要思考時完全不動。
    reasoning_budget_low: float = 1.2
    reasoning_budget_high: float = 1.8
    # 記憶抽取的前置閘：Jev 說「這段對話沒有值得記的事實」時，跳過那次抽取
    # 呼叫。門檻刻意設得低 —— 漏記一則正確的事實，比多花一次便宜呼叫嚴重
    # 得多，所以這個閘只可以在有把握時才收窄。
    memory_decision_threshold: float = 0.25

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
    # 一次送幾格。三格足以看出「這條片在做什麼」，再多只是重複同樣的畫面。
    # 每一格約 170 token，三格約 510。
    media_frames: int = 3
    # 超過這個長度就只取第一格 —— 完整解碼一段長片會讓回覆延遲到無法接受。
    # 讀少一格，總比讓使用者等半分鐘好。
    media_max_seconds: float = 180.0

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
