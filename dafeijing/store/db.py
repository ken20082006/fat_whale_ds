"""SQLite 存取層。

單一連線 + 寫入鎖。這個 bot 的併發量很低（朋友數量級），
單連線足以應付，且免去連線池的複雜度。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Sequence

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class Database:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # ── 生命週期 ────────────────────────────────────────

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        await self._conn.commit()
        logger.info("資料庫就緒：%s", self._path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("資料庫尚未連線，請先呼叫 connect()")
        return self._conn

    # ── 查詢 ────────────────────────────────────────────

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        async with self._lock:
            async with self.conn.execute(sql, params) as cursor:
                return await cursor.fetchone()

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        async with self._lock:
            async with self.conn.execute(sql, params) as cursor:
                return list(await cursor.fetchall())

    async def fetchval(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = await self.fetchone(sql, params)
        return row[0] if row is not None else default

    # ── 寫入 ────────────────────────────────────────────

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """執行單筆寫入，回傳 lastrowid（INSERT 用）。"""
        async with self._lock:
            cursor = await self.conn.execute(sql, params)
            await self.conn.commit()
            return cursor.lastrowid or 0

    async def affect(self, sql: str, params: Sequence[Any] = ()) -> int:
        """執行寫入並回傳受影響列數（DELETE / UPDATE 用）。"""
        async with self._lock:
            cursor = await self.conn.execute(sql, params)
            await self.conn.commit()
            return cursor.rowcount

    async def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        async with self._lock:
            await self.conn.executemany(sql, seq)
            await self.conn.commit()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """多筆寫入需具原子性時使用。"""
        async with self._lock:
            try:
                yield self.conn
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise

    # ── 維護 ────────────────────────────────────────────

    async def purge_expired(
        self,
        history_retention_days: int,
        group_cache_retention_hours: int,
    ) -> dict[str, int]:
        """清除逾期的對話原文與群組快取。回傳各表刪除筆數。"""
        counts: dict[str, int] = {}

        counts["messages"] = await self.affect(
            "DELETE FROM messages WHERE created_at < datetime('now', ?)",
            (f"-{history_retention_days} days",),
        )
        counts["group_cache"] = await self.affect(
            "DELETE FROM group_cache WHERE created_at < datetime('now', ?)",
            (f"-{group_cache_retention_hours} hours",),
        )

        return counts

    async def backup_to(self, dest: str | Path) -> None:
        """用 SQLite 官方的 backup API 產出一致性快照。"""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            target = await aiosqlite.connect(dest)
            try:
                await self.conn.backup(target)
                await target.commit()
            finally:
                await target.close()
        logger.info("備份完成：%s", dest)
