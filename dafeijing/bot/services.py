"""執行期共用物件。

handler 透過 context.bot_data["services"] 取得，避免到處傳參數。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.access import AccessControl, MembershipCache
from ..core.chat import ChatService
from ..core.chain import ReplyChain
from ..core.debounce import Debouncer
from ..core.memory import MemoryExtractor
from ..core.persona import Persona
from ..core.ratelimit import RateLimiter
from ..core.session import SessionManager
from ..core.usage import UsageLog
from ..llm.openrouter import OpenRouterClient
from ..settings import Settings
from ..store.db import Database


@dataclass
class Services:
    cfg: Settings
    db: Database
    access: AccessControl
    sessions: SessionManager
    chain: ReplyChain
    persona: Persona
    llm: OpenRouterClient
    usage: UsageLog
    chat: ChatService
    debouncer: Debouncer
    limiter: RateLimiter
    group_access: MembershipCache
    memory: MemoryExtractor

    bot_username: str = ""
    bot_id: int = 0
    bot_name: str = "大肥鯨"
    started_at: float = 0.0
    errors: int = field(default=0)

    def is_admin(self, tg_user_id: int | None) -> bool:
        return self.cfg.is_admin(tg_user_id)

    def mention(self) -> str:
        return f"@{self.bot_username}" if self.bot_username else self.bot_name
