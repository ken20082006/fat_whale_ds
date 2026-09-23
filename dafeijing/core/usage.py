"""用量記錄。

目前只記錄不封鎖 —— 先跑一兩週看實際數字，再決定配額。
所有查詢都以 usage_log 為單一真相來源。
"""

from __future__ import annotations

import logging

from ..store.db import Database
from .util import now_iso

logger = logging.getLogger(__name__)


class UsageLog:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(
        self,
        *,
        user_id: int | None,
        chat_id: int | None,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cached_tokens: int = 0,
        reasoning_tokens: int = 0,
        image_tokens: int = 0,
        cost: float = 0.0,
    ) -> None:
        await self._db.execute(
            "INSERT INTO usage_log (user_id, chat_id, model, prompt_tokens, completion_tokens, "
            "cached_tokens, reasoning_tokens, image_tokens, cost, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                chat_id,
                model,
                prompt_tokens,
                completion_tokens,
                cached_tokens,
                reasoning_tokens,
                image_tokens,
                cost,
                now_iso(),
            ),
        )

    # ── 查詢 ────────────────────────────────────────────

    async def user_today(self, user_id: int) -> dict:
        return await self._aggregate("AND user_id = ?", (user_id,), days=1)

    async def user_summary(self, user_id: int, days: int = 30) -> dict:
        return await self._aggregate("AND user_id = ?", (user_id,), days=days)

    async def overall(self, days: int = 7) -> dict:
        return await self._aggregate("", (), days=days)

    async def top_users(self, days: int = 7, limit: int = 10) -> list:
        return await self._db.fetchall(
            "SELECT u.display_name, u.tg_user_id, "
            "COUNT(*) AS calls, "
            "SUM(l.prompt_tokens + l.completion_tokens) AS tokens, "
            "SUM(l.cost) AS cost "
            "FROM usage_log l LEFT JOIN users u ON u.tg_user_id = l.user_id "
            "WHERE l.created_at >= datetime('now', ?) "
            "GROUP BY l.user_id ORDER BY tokens DESC LIMIT ?",
            (f"-{days} days", limit),
        )

    async def _aggregate(self, extra_where: str, params: tuple, days: int) -> dict:
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS calls, "
            "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
            "COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
            "COALESCE(SUM(cached_tokens), 0) AS cached_tokens, "
            "COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens, "
            "COALESCE(SUM(image_tokens), 0) AS image_tokens, "
            "COALESCE(SUM(cost), 0) AS cost "
            f"FROM usage_log WHERE created_at >= datetime('now', ?) {extra_where}",
            (f"-{days} days", *params),
        )
        if row is None:
            return {}
        data = dict(row)
        data["total_tokens"] = data["prompt_tokens"] + data["completion_tokens"]
        return data
