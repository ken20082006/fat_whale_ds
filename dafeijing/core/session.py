"""Session 管理：三層記憶。

短期 —— 最近 N 輪原文（滑動視窗）
中期 —— 滾動摘要，隨壓縮更新，上限數百 token
長期 —— memory_notes，跨 session 永久保存

設計要點：閒置重置與 /new 都採「軟重置」——把視窗壓進摘要後清空原文，
摘要本身沿用。使用者感覺得到連貫性，成本卻回到最低。只有 /reset 會連摘要一起清掉。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from ..store.db import Database
from .tokens import estimate_tokens
from .util import in_minutes, now_iso

logger = logging.getLogger(__name__)

SummarizerFn = Callable[[str], Awaitable[str]]

PRIVATE_SCOPE = "private"
GROUP_SCOPE = "group"


def scope_for(is_group: bool) -> str:
    """長期筆記的命名空間，只有兩種。

    - `private`：私聊。裡面的東西永遠不會在群組出現。
    - `group`：**所有群組共用一份**。你在 A 群講的事，在 B 群也會被記得。

    私聊獨立是刻意的：朋友私下說過「我最近失業」，不該在群組被提起。
    群組之間則是同一個人的公開面，沒有分開的理由。
    """
    return GROUP_SCOPE if is_group else PRIVATE_SCOPE


@dataclass(frozen=True)
class SessionRef:
    id: int
    chat_key: str
    kind: str
    summary: str | None
    summary_tokens: int
    last_active_at: str


class SessionManager:
    def __init__(self, db: Database, cfg) -> None:
        self._db = db
        self._cfg = cfg

    # ── 取得或建立 ──────────────────────────────────────

    async def private_session(self, tg_user_id: int) -> SessionRef:
        """取得私聊 session。若閒置過久，先做軟重置。"""
        chat_key = f"private:{tg_user_id}"
        row = await self._db.fetchone("SELECT * FROM sessions WHERE chat_key = ?", (chat_key,))

        if row is None:
            session_id = await self._db.execute(
                "INSERT INTO sessions (chat_key, kind, user_id, chat_id, last_active_at, created_at) "
                "VALUES (?, 'private', ?, ?, ?, ?)",
                (chat_key, tg_user_id, tg_user_id, now_iso(), now_iso()),
            )
            row = await self._db.fetchone("SELECT * FROM sessions WHERE id = ?", (session_id,))

        session = self._row_to_ref(row)
        if await self._is_idle(session, self._cfg.idle_reset_minutes):
            logger.info("session %s 閒置逾時，執行軟重置", chat_key)
            await self.soft_reset(session.id)
            row = await self._db.fetchone("SELECT * FROM sessions WHERE id = ?", (session.id,))
            session = self._row_to_ref(row)
        return session

    async def group_thread_session(self, chat_id: int, root_message_id: int) -> SessionRef:
        """群組以「引用串」為單位。同一條串共用一個 session。"""
        chat_key = f"group:{chat_id}:thread:{root_message_id}"
        row = await self._db.fetchone("SELECT * FROM sessions WHERE chat_key = ?", (chat_key,))

        if row is None:
            session_id = await self._db.execute(
                "INSERT INTO sessions (chat_key, kind, chat_id, thread_root_id, "
                "last_active_at, created_at) VALUES (?, 'group_thread', ?, ?, ?, ?)",
                (chat_key, chat_id, root_message_id, now_iso(), now_iso()),
            )
            row = await self._db.fetchone("SELECT * FROM sessions WHERE id = ?", (session_id,))
            return self._row_to_ref(row)

        session = self._row_to_ref(row)
        if await self._is_idle(session, self._cfg.group_thread_ttl_minutes):
            logger.info("群組串 %s 逾時，重新開始", chat_key)
            await self.soft_reset(session.id)
            row = await self._db.fetchone("SELECT * FROM sessions WHERE id = ?", (session.id,))
            session = self._row_to_ref(row)
        return session

    # ── 讀取 ────────────────────────────────────────────

    async def window(self, session_id: int) -> list[dict]:
        """最近 N 輪的原文訊息，依時間正序。"""
        limit = self._cfg.window_turns * 2
        rows = await self._db.fetchall(
            "SELECT role, content, tokens FROM messages WHERE session_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
        return [dict(row) for row in reversed(rows)]

    async def recent_messages(self, session_id: int, limit: int) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
        return [dict(row) for row in reversed(rows)]

    async def window_tokens(self, session_id: int) -> int:
        rows = await self._db.fetchall(
            "SELECT tokens FROM messages WHERE session_id = ?", (session_id,)
        )
        return sum(row["tokens"] or 0 for row in rows)

    # ── 寫入 ────────────────────────────────────────────

    async def append(self, session_id: int, role: str, content: str, has_image: bool = False) -> None:
        await self._db.execute(
            "INSERT INTO messages (session_id, role, content, tokens, has_image, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, role, content, estimate_tokens(content), 1 if has_image else 0, now_iso()),
        )
        await self._db.execute(
            "UPDATE sessions SET last_active_at = ? WHERE id = ?", (now_iso(), session_id)
        )

    async def undo_last_turn(self, session_id: int) -> int:
        """刪除最後一輪（一問一答）。回傳刪除的訊息數。"""
        rows = await self._db.fetchall(
            "SELECT id, role FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 2",
            (session_id,),
        )
        if not rows:
            return 0
        ids = [row["id"] for row in rows]
        placeholders = ",".join("?" for _ in ids)
        await self._db.affect(f"DELETE FROM messages WHERE id IN ({placeholders})", ids)
        return len(ids)

    async def clear_window(self, session_id: int) -> int:
        return await self._db.affect(
            "DELETE FROM messages WHERE session_id = ?", (session_id,)
        )

    async def set_summary(self, session_id: int, summary: str | None) -> None:
        await self._db.execute(
            "UPDATE sessions SET summary = ?, summary_tokens = ? WHERE id = ?",
            (summary, estimate_tokens(summary), session_id),
        )

    # ── 重置 ────────────────────────────────────────────

    async def soft_reset(self, session_id: int, summarizer: SummarizerFn | None = None) -> None:
        """把視窗內容壓進摘要後清空原文。摘要沿用，連貫性保留。"""
        session = await self._db.fetchone("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if session is None:
            return

        window = await self.window(session_id)
        if window and summarizer is not None:
            summary = await self._run_summarizer(
                summarizer, session["summary"], window, force=True
            )
            await self.set_summary(session_id, summary)
        elif not window:
            await self.set_summary(session_id, session["summary"])

        await self.clear_window(session_id)
        await self._db.execute(
            "UPDATE sessions SET last_active_at = ? WHERE id = ?", (now_iso(), session_id)
        )

    async def hard_reset(self, session_id: int) -> None:
        """連摘要一起清掉。長期記憶不受影響。"""
        await self.clear_window(session_id)
        await self.set_summary(session_id, None)
        await self._db.execute(
            "UPDATE sessions SET last_active_at = ? WHERE id = ?", (now_iso(), session_id)
        )

    # ── 壓縮 ────────────────────────────────────────────

    async def maybe_compact(self, session_id: int, summarizer: SummarizerFn) -> bool:
        """視窗過大時，把最舊的一半併入摘要。回傳是否真的壓縮了。"""
        total = await self.window_tokens(session_id)
        if total <= self._cfg.compact_trigger_tokens:
            return False

        rows = await self._db.fetchall(
            "SELECT id, role, content FROM messages WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        )
        if len(rows) <= self._cfg.window_turns:
            return False

        keep = self._cfg.window_turns
        older = rows[:-keep]
        if not older:
            return False

        session = await self._db.fetchone("SELECT * FROM sessions WHERE id = ?", (session_id,))
        old_summary = session["summary"] if session else None
        summary = await self._run_summarizer(
            summarizer, old_summary, [dict(row) for row in older], force=True
        )
        if not summary:
            return False

        ids = [row["id"] for row in older]
        placeholders = ",".join("?" for _ in ids)
        await self._db.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", ids)
        await self.set_summary(session_id, summary)
        logger.info("session %s 已壓縮 %d 則訊息進摘要", session_id, len(ids))
        return True

    async def _run_summarizer(
        self,
        summarizer: SummarizerFn,
        old_summary: str | None,
        messages: list[dict],
        force: bool = False,
    ) -> str | None:
        transcript = "\n".join(
            f"{'使用者' if m['role'] == 'user' else '大肥鯨'}：{m['content']}" for m in messages
        )
        prompt = _SUMMARY_PROMPT.format(
            old_summary=old_summary or "（無）",
            transcript=transcript,
            limit=self._cfg.summary_max_tokens,
        )
        try:
            summary = (await summarizer(prompt)).strip()
        except Exception:
            logger.exception("摘要失敗，保留原摘要")
            return old_summary
        return summary or old_summary

    # ── 長期記憶 ────────────────────────────────────────

    async def notes(
        self,
        tg_user_id: int,
        scope: str = PRIVATE_SCOPE,
        limit: int | None = None,
    ) -> list[str]:
        """由舊到新。limit 為 None 時取 cfg 的上限；整理筆記時要傳大一點。"""
        rows = await self._db.fetchall(
            "SELECT content FROM memory_notes WHERE user_id = ? AND scope = ? "
            "ORDER BY id DESC LIMIT ?",
            (tg_user_id, scope, limit or self._cfg.notes_per_scope_max),
        )
        return [row["content"] for row in reversed(rows)]

    async def replace_notes(self, tg_user_id: int, scope: str, contents: list[str]) -> int:
        """整批換掉某個場合的筆記。給定期整理用。"""
        cleaned = [" ".join(item.split())[:500] for item in contents]
        cleaned = [item for item in cleaned if item][: self._cfg.notes_per_scope_max]
        if not cleaned:
            return 0

        async with self._db.transaction() as conn:
            await conn.execute(
                "DELETE FROM memory_notes WHERE user_id = ? AND scope = ?",
                (tg_user_id, scope),
            )
            await conn.executemany(
                "INSERT INTO memory_notes (user_id, scope, content, source, created_at) "
                "VALUES (?, ?, ?, 'summary', ?)",
                [(tg_user_id, scope, item, now_iso()) for item in cleaned],
            )
        return len(cleaned)

    async def add_note(
        self,
        tg_user_id: int,
        content: str,
        *,
        scope: str = PRIVATE_SCOPE,
        source: str = "explicit",
    ) -> bool:
        """回傳是否真的寫入（空白或重複會被擋下）。"""
        content = " ".join(content.split())[:500]
        if not content:
            return False
        if await self._is_duplicate(tg_user_id, scope, content):
            return False

        await self._db.execute(
            "INSERT INTO memory_notes (user_id, scope, content, source, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (tg_user_id, scope, content, source, now_iso()),
        )
        await self._trim_notes(tg_user_id, scope)
        return True

    async def _is_duplicate(self, tg_user_id: int, scope: str, content: str) -> bool:
        existing = await self._db.fetchval(
            "SELECT 1 FROM memory_notes WHERE user_id = ? AND scope = ? AND content = ?",
            (tg_user_id, scope, content),
            default=None,
        )
        return existing is not None

    async def _trim_notes(self, tg_user_id: int, scope: str) -> None:
        """超過上限就丟掉最舊的，避免筆記無限累積把 system prompt 撐大。"""
        limit = self._cfg.notes_per_scope_max
        await self._db.affect(
            "DELETE FROM memory_notes WHERE user_id = ? AND scope = ? AND id NOT IN ("
            "SELECT id FROM memory_notes WHERE user_id = ? AND scope = ? "
            "ORDER BY id DESC LIMIT ?)",
            (tg_user_id, scope, tg_user_id, scope, limit),
        )

    async def clear_notes(self, tg_user_id: int, scope: str | None = None) -> int:
        """scope 為 None 時清掉所有場合的筆記。"""
        if scope is None:
            return await self._db.affect(
                "DELETE FROM memory_notes WHERE user_id = ?", (tg_user_id,)
            )
        return await self._db.affect(
            "DELETE FROM memory_notes WHERE user_id = ? AND scope = ?",
            (tg_user_id, scope),
        )

    async def note_counts(self, tg_user_id: int) -> list[tuple[str, int]]:
        """各場合的筆記數量，供 /context 顯示。"""
        rows = await self._db.fetchall(
            "SELECT scope, COUNT(*) AS n FROM memory_notes WHERE user_id = ? "
            "GROUP BY scope ORDER BY n DESC",
            (tg_user_id,),
        )
        return [(row["scope"], row["n"]) for row in rows]

    # ── 內部 ────────────────────────────────────────────

    async def _is_idle(self, session: SessionRef, minutes: int) -> bool:
        idle = await self._db.fetchval(
            "SELECT ? < datetime('now', ?)", (session.last_active_at, f"-{minutes} minutes")
        )
        if not idle:
            return False
        # 已經空的 session 不必再重置
        has_window = await self._db.fetchval(
            "SELECT 1 FROM messages WHERE session_id = ? LIMIT 1", (session.id,), default=None
        )
        return has_window is not None

    @staticmethod
    def _row_to_ref(row) -> SessionRef:
        return SessionRef(
            id=row["id"],
            chat_key=row["chat_key"],
            kind=row["kind"],
            summary=row["summary"],
            summary_tokens=row["summary_tokens"],
            last_active_at=row["last_active_at"],
        )


_SUMMARY_PROMPT = """\
你在為一段對話做滾動摘要。這份摘要會取代被淘汰的舊訊息，成為後續對話的唯一記憶。

規則：
- 用第三人稱記述，不要寫成對話。
- 保留：使用者的目標與偏好、已達成的結論、未解決的問題、重要的技術細節與決定、使用者透露的個人事實。
- 丟棄：寒暄、重複內容、已被推翻的猜測。
- 不要評判，不要加建議。
- 不要寫「使用者問了…」這種流水帳，直接寫事實。
- 篇幅上限約 {limit} token，寧可精簡。

既有摘要：
{old_summary}

新增的對話內容：
{transcript}

請輸出合併後的新摘要。"""
