"""Router 嘅執行期物件。

同大肥鯨嘅 `bot/services.py` 幾乎一樣，**除咗對話本身唔喺度**：
冇 ChatService、冇人設 prompt 組裝、冇 session 摘要 ——
對話交咗畀 Hermes。

但**記憶同判斷留返喺 Router**：
- `sessions` / `memory` —— per-user 筆記。Hermes 嗰份 `USER.md` 係 profile
  全域，冇 per-sender 概念，所以呢件事一定要自己做（見 router/memory.py）。
- `llm` / `decisions` —— 只為咗背景抽取筆記而存在。呢個係 utility 工作，
  唔係對話：用 Hermes 做要成 17k tokens 一次，用 OpenRouter 直打係千幾。
  對話本身仍然 100% 經 Hermes。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.access import AccessControl, MembershipCache
from ..core.chain import ReplyChain
from ..core.groupprofile import GroupProfiler
from ..core.memory import MemoryExtractor
from ..core.ratelimit import RateLimiter
from ..core.session import SessionManager
from ..core.stickers import StickerLibrary
from ..llm.decisions import DecisionsClient
from ..llm.openrouter import OpenRouterClient
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
    sessions: SessionManager
    llm: OpenRouterClient
    decisions: DecisionsClient
    memory: MemoryExtractor
    profiler: GroupProfiler
    stickers: StickerLibrary

    bot_username: str = ""
    bot_id: int = 0
    bot_name: str = "大肥鯨"
    started_at: float = 0.0
    errors: int = field(default=0)
    # 邊幾條對話已經附過貼圖清單。純記憶體 —— 重啟後每條再附一次，無害。
    seen_conversations: set[str] = field(default_factory=set)
    # 私聊嘅「開新對話」世代。/new 會加一，令對話名接唔返上一條。
    dm_generation: int = 0

    def is_admin(self, tg_user_id: int | None) -> bool:
        return self.cfg.is_admin(tg_user_id)

    def mention(self) -> str:
        return f"@{self.bot_username}" if self.bot_username else self.bot_name


def create_services(cfg: Settings) -> RouterServices:
    """砌齊所有依賴。刻意同大肥鯨嗰個同名同形狀 —— 方便對照。"""
    db = Database(cfg.db_path)
    sessions = SessionManager(db, cfg)
    llm = OpenRouterClient(cfg)
    decisions = DecisionsClient(cfg)

    return RouterServices(
        cfg=cfg,
        db=db,
        access=AccessControl(db),
        chain=ReplyChain(db, cfg),
        limiter=RateLimiter(cfg.rate_per_minute),
        group_access=MembershipCache(ttl_seconds=cfg.group_membership_ttl_seconds),
        hermes=HermesClient(cfg.hermes_url, cfg.hermes_key),
        sessions=sessions,
        llm=llm,
        decisions=decisions,
        memory=MemoryExtractor(cfg, sessions, llm, decisions),
        # 群組概況：每個群一則氣氛/慣例。同筆記一樣要自己排程更新，
        # 因為 Hermes 嗰邊冇呢個概念。
        profiler=GroupProfiler(cfg, sessions, llm),
        # 貼圖庫由 app.py 嘅 post_init 載入（要等 db 連上先讀得到）。
        stickers=StickerLibrary(cfg),
    )