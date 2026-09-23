"""集中設定。所有可調參數都在這裡，不散落在各模組。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    model: str = "deepseek/deepseek-chat"
    model_vision: str = "deepseek/deepseek-chat"
    model_utility: str = "deepseek/deepseek-chat"

    # 這個模型預設會做推理（reasoning），推理 token 以輸出計價且會佔用 max_tokens。
    # 實測關掉後單次費用約降為四分之一，對一般閒聊毫無損失。
    # 使用者可用 /think 個別開啟。
    reasoning_enabled: bool = False

    # ── 路徑 ──
    persona_file: Path = Path("config/persona.md")
    db_path: Path = Path("data/fatwhale.db")
    log_dir: Path = Path("logs")

    # ── 授權 ──
    admin_user_ids: str = ""
    invite_ttl_seconds: int = 300  # 邀請碼有效期，預設五分鐘

    # ── Session ──
    # 模型有 1M 上下文，且輸入每百萬 token 僅 $0.1，所以視窗可以開得比一般保守值大。
    # 30 輪約一萬多 token，每個請求的輸入成本仍在千分之一美元量級。
    window_turns: int = 30
    compact_trigger_tokens: int = 12_000
    summary_max_tokens: int = 800
    idle_reset_minutes: int = 480
    # max_tokens 是上限而非預留，設寬不會多花錢，但能避免長回答或推理被截斷
    private_reply_max_tokens: int = 8000
    group_reply_max_tokens: int = 4000
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

    # ── 圖片 ──
    image_max_edge: int = 1024
    image_max_bytes: int = 8 * 1024 * 1024

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

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
