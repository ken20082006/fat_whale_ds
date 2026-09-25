"""Router 嘅執行期物件。

同大肥鯨嘅 `bot/services.py` 幾乎一樣，**除咗冇咗「腦」**：
冇 ChatService、冇 OpenRouter client、冇 Jev、冇記憶抽取器。
嗰啲全部搬咗去 Hermes。剩返嘅係外殼：授權、快取、引用串、渲染、節流。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.access import AccessControl, MembershipCache
from ..core.chain import ReplyChain
from ..core.ratelimit import RateLimiter
from ..settings import Settings
from ..store.db import Database
from .hermes import HermesClient


@dataclass
class RouterServices:
    cfg: Settings
    db: Database
    access: AccessControl
    chain: ReplyChain
    limiter: RateLimiter
    group_access: MembershipCache
    hermes: HermesClient

    bot_username: str = ""
    bot_id: int = 0
    bot_name: str = "大肥鯨"
    started_at: float = 0.0
    errors: int = field(default=0)

    def is_admin(self, tg_user_id: int | None) -> bool:
        return self.cfg.is_admin(tg_user_id)

    def mention(self) -> str:
        return f"@{self.bot_username}" if self.bot_username else self.bot_name


def create_services(cfg: Settings) -> RouterServices:
    """砌齊所有依賴。刻意同大肥鯨嗰個同名同形狀 —— 方便日後對照。"""
    db = Database(cfg.db_path)
    return RouterServices(
        cfg=cfg,
        db=db,
        access=AccessControl(db),
        chain=ReplyChain(db, cfg),
        limiter=RateLimiter(cfg.rate_per_minute),
        group_access=MembershipCache(ttl_seconds=cfg.group_membership_ttl_seconds),
        hermes=HermesClient(cfg.hermes_url, cfg.hermes_key),
    )
