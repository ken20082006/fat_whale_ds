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

    # ── 路徑 ──
    persona_file: Path = Path("config/persona.md")
    db_path: Path = Path("data/fatwhale.db")
    log_dir: Path = Path("logs")

    # ── 授權 ──
    admin_user_ids: str = ""
    invite_ttl_seconds: int = 300  # 邀請碼有效期，預設五分鐘

    # ── Session ──
    window_turns: int = 12
    compact_trigger_tokens: int = 3000
    summary_max_tokens: int = 400
    idle_reset_minutes: int = 120
    private_reply_max_tokens: int = 1000
    group_reply_max_tokens: int = 400
    group_chain_max_messages: int = 20
    group_chain_max_tokens: int = 3000
    group_thread_ttl_minutes: int = 360

    # ── 節流 ──
    debounce_seconds: float = 1.5
    rate_per_minute: int = 5

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
