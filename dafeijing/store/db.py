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

# 後續版本新增的欄位。舊資料庫啟動時會自動補上。
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("users", "reasoning", "INTEGER"),
    ("usage_log", "reasoning_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("memory_notes", "scope", "TEXT NOT NULL DEFAULT 'private'"),
)


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
        await self._add_missing_columns()
        await self._conn.commit()
        await self._migrate_data()
        logger.info("資料庫就緒：%s", self._path)

    async def _migrate_data(self) -> None:
        """一次性資料搬移。每項都要能重複執行而不出錯。"""
        # 舊版的長期筆記以 'group:<chat_id>' 一個群組一份，
        # 現在改成所有群組共用一份。
        merged = await self.affect(
            "UPDATE memory_notes SET scope = 'group' WHERE scope LIKE 'group:%'"
        )
        if merged:
            logger.info("長期筆記場合合併：%d 則改為群組共用", merged)

            # 合併後同一個人可能在不同群組記過同一件事，去重
            removed = await self.affect(
                "DELETE FROM memory_notes WHERE id NOT IN ("
                "SELECT MIN(id) FROM memory_notes GROUP BY user_id, scope, content)"
            )
            if removed:
                logger.info("長期筆記去重：移除 %d 則重複", removed)

    async def _add_missing_columns(self) -> None:
        """既有的資料庫不會因為新增欄位而重建，這裡補上缺的。

        CREATE TABLE IF NOT EXISTS 只對新庫生效，舊庫要自己 ALTER。
        """
        assert self._conn is not None
        for table, column, definition in _ADDED_COLUMNS:
            async with self._conn.execute(f"PRAGMA table_info({table})") as cursor:
                existing = {row[1] for row in await cursor.fetchall()}
            if not existing:
                continue  # 這張表還不存在，schema 會處理
            if column in existing:
                continue
            await self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            logger.info("資料庫補上欄位：%s.%s", table, column)

    async def close(self) -> None:
        if self._conn is None:
            return
        conn, self._conn = self._conn, None
        try:
            # 把 WAL 併回主檔並清空。否則會留下 -wal / -shm，
            # 在 Windows 上這些檔案常被鎖住一段時間，刪除暫存目錄時會失敗。
            await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            logger.debug("WAL checkpoint 失敗，不影響關閉")
        await conn.close()

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
