"""群組引用鏈回溯。

Bot API 沒有「依 message_id 取訊息」的方法，`reply_to_message` 也只帶上一層。
因此要還原整條引用串，只能自己快取群組訊息。快取僅供回溯使用，
除了被指名的那條串，其餘內容永遠不會送進模型，且逾時自動清除。
"""

from __future__ import annotations

import logging

from ..store.db import Database
from .tokens import estimate_tokens
from .util import now_iso

logger = logging.getLogger(__name__)

_MEDIA_PLACEHOLDER = "（非文字訊息）"
_ELISION = "……（中間省略 {n} 則）"


class ReplyChain:
    def __init__(self, db: Database, cfg) -> None:
        self._db = db
        self._cfg = cfg

    # ── 快取 ────────────────────────────────────────────

    async def cache_message(
        self,
        chat_id: int,
        message_id: int,
        reply_to_id: int | None,
        user_id: int | None,
        display_name: str | None,
        text: str | None,
        has_media: bool = False,
    ) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO group_cache "
            "(chat_id, message_id, reply_to_id, user_id, display_name, text, has_media, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chat_id,
                message_id,
                reply_to_id,
                user_id,
                display_name,
                text or "",
                1 if has_media else 0,
                now_iso(),
            ),
        )

    async def cache_from_update(self, message) -> None:
        """從 telegram.Message 萃取並存入快取。呼叫端需自行判斷是否為群組訊息。"""
        if message is None or message.chat is None:
            return

        sender = message.from_user
        display_name = None
        if sender is not None:
            display_name = sender.full_name or sender.username

        text = message.text or message.caption
        has_media = text is None

        await self.cache_message(
            chat_id=message.chat_id,
            message_id=message.message_id,
            reply_to_id=message.reply_to_message.message_id if message.reply_to_message else None,
            user_id=sender.id if sender else None,
            display_name=display_name,
            text=text,
            has_media=has_media,
        )

    async def purge(self) -> int:
        return await self._db.affect(
            "DELETE FROM group_cache WHERE created_at < datetime('now', ?)",
            (f"-{self._cfg.group_cache_retention_hours} hours",),
        )

    # ── 回溯 ────────────────────────────────────────────

    async def resolve(self, chat_id: int, leaf_message_id: int) -> list[dict]:
        """從被指名的那則往上追溯至串的源頭，回傳由舊到新的列表。"""
        chain: list[dict] = []
        seen: set[int] = set()
        current: int | None = leaf_message_id
        limit = self._cfg.group_chain_max_messages

        while current is not None and len(chain) < limit:
            if current in seen:
                break
            seen.add(current)

            row = await self._db.fetchone(
                "SELECT message_id, reply_to_id, user_id, display_name, text, has_media "
                "FROM group_cache WHERE chat_id = ? AND message_id = ?",
                (chat_id, current),
            )
            if row is None:
                break
            chain.append(dict(row))
            current = row["reply_to_id"]

        chain.reverse()
        return self._trim(chain)

    def _trim(self, chain: list[dict]) -> list[dict]:
        """超出 token 預算時保留頭尾、摺疊中段。"""
        budget = self._cfg.group_chain_max_tokens
        if not chain:
            return chain

        def total(items: list[dict]) -> int:
            return sum(estimate_tokens(item.get("text")) + 8 for item in items)

        if total(chain) <= budget:
            return chain

        head: list[dict] = []
        tail: list[dict] = []
        head_budget = budget // 3
        tail_budget = budget - head_budget

        used = 0
        for item in chain:
            cost = estimate_tokens(item.get("text")) + 8
            if used + cost > head_budget:
                break
            head.append(item)
            used += cost

        used = 0
        for item in reversed(chain):
            cost = estimate_tokens(item.get("text")) + 8
            if used + cost > tail_budget:
                break
            tail.append(item)
            used += cost
        tail.reverse()

        omitted = len(chain) - len(head) - len(tail)
        marker = {
            "message_id": -1,
            "display_name": None,
            "text": _ELISION.format(n=omitted),
            "has_media": False,
            "reply_to_id": None,
            "user_id": None,
        }
        if omitted > 0:
            return head + [marker] + tail
        return chain

    @staticmethod
    def format_for_prompt(chain: list[dict], bot_name: str) -> str:
        """把引用串排成模型看得懂的結構。"""
        if not chain:
            return ""
        lines = ["以下是這條引用串的完整內容，由舊到新："]
        for item in chain:
            speaker = item.get("display_name") or "某人"
            text = item.get("text") or _MEDIA_PLACEHOLDER
            if item.get("message_id") == -1:
                lines.append(text)
            else:
                lines.append(f"【{speaker}】{text}")
        lines.append(f"\n（{bot_name} 是被指名回應的那一方）")
        return "\n".join(lines)
