"""存取控制：邀請碼、使用者狀態、群組白名單。

邀請連結採 Telegram 深連結，朋友點一下即完成綁定：
    https://t.me/<bot>?start=DFJ-7K2M-9QX4
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from enum import Enum

from ..store.db import Database
from .util import in_seconds, now_iso

logger = logging.getLogger(__name__)

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉 I O 0 1
_PREFIX = "DFJ"


class RedeemStatus(str, Enum):
    OK = "ok"
    INVALID = "invalid"
    REVOKED = "revoked"
    EXPIRED = "expired"
    ALREADY_BOUND = "already_bound"
    ALREADY_ACTIVE = "already_active"


@dataclass(frozen=True)
class RedeemResult:
    status: RedeemStatus
    label: str | None = None


def generate_code() -> str:
    """產生 DFJ-XXXX-XXXX 形式的邀請碼（明文只出現一次）。"""
    block = lambda: "".join(secrets.choice(_ALPHABET) for _ in range(4))  # noqa: E731
    return f"{_PREFIX}-{block()}-{block()}"


def hash_code(code: str) -> str:
    """一律先正規化再雜湊，避免 "DFJ 7K2M 9QX4" 與 "DFJ-7K2M-9QX4" 算出不同值。"""
    normalised = normalise_code(code)
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def normalise_code(raw: str) -> str:
    """容忍使用者手動輸入時的各種寫法：dfj7k2m9qx4、DFJ 7K2M 9QX4 等。"""
    cleaned = raw.strip().upper().replace(" ", "").replace("_", "-")
    if cleaned.startswith(_PREFIX):
        cleaned = cleaned[len(_PREFIX) :]
    cleaned = cleaned.lstrip("-").replace("-", "")
    if len(cleaned) != 8:
        return raw.strip().upper().replace(" ", "")
    return f"{_PREFIX}-{cleaned[:4]}-{cleaned[4:]}"


class AccessControl:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ── 使用者 ──────────────────────────────────────────

    async def get_user(self, tg_user_id: int):
        return await self._db.fetchone(
            "SELECT * FROM users WHERE tg_user_id = ?", (tg_user_id,)
        )

    async def get_user_by_pk(self, user_pk: int):
        return await self._db.fetchone("SELECT * FROM users WHERE id = ?", (user_pk,))

    async def is_active(self, tg_user_id: int) -> bool:
        status = await self._db.fetchval(
            "SELECT status FROM users WHERE tg_user_id = ?", (tg_user_id,)
        )
        return status == "active"

    async def touch(self, tg_user_id: int, display_name: str | None, username: str | None) -> None:
        """更新最後活動時間與顯示名稱。"""
        await self._db.execute(
            "UPDATE users SET last_seen_at = ?, display_name = COALESCE(?, display_name), "
            "username = COALESCE(?, username) WHERE tg_user_id = ?",
            (now_iso(), display_name, username, tg_user_id),
        )

    async def set_vibe(self, tg_user_id: int, vibe: str) -> None:
        await self._db.execute(
            "UPDATE users SET vibe = ? WHERE tg_user_id = ?", (vibe, tg_user_id)
        )

    async def set_status(self, tg_user_id: int, status: str) -> None:
        await self._db.execute(
            "UPDATE users SET status = ? WHERE tg_user_id = ?", (status, tg_user_id)
        )

    # ── 邀請碼 ──────────────────────────────────────────

    async def issue_invite(
        self,
        label: str | None,
        created_by: int,
        ttl_seconds: int = 300,
    ) -> str:
        """核發邀請碼，回傳明文。明文不會入庫，且短時間內失效。"""
        code = generate_code()
        await self._db.execute(
            "INSERT INTO invites (code_hash, label, expires_at, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                hash_code(code),
                label,
                in_seconds(ttl_seconds) if ttl_seconds else None,
                now_iso(),
                created_by,
            ),
        )
        logger.info("核發邀請碼：%s（%s 秒內有效，備註：%s）", code, ttl_seconds, label or "無")
        return code

    async def invite_expiry(self, code: str) -> str | None:
        return await self._db.fetchval(
            "SELECT expires_at FROM invites WHERE code_hash = ?",
            (hash_code(normalise_code(code)),),
            default=None,
        )

    async def revoke_invite(self, code: str) -> bool:
        row = await self._db.fetchone(
            "SELECT id FROM invites WHERE code_hash = ?", (hash_code(normalise_code(code)),)
        )
        if row is None:
            return False
        await self._db.execute("UPDATE invites SET revoked = 1 WHERE id = ?", (row["id"],))
        return True

    async def list_invites(self, limit: int = 25) -> list:
        return await self._db.fetchall(
            "SELECT * FROM invites ORDER BY id DESC LIMIT ?", (limit,)
        )

    async def redeem(
        self,
        tg_user_id: int,
        raw_code: str,
        display_name: str | None = None,
        username: str | None = None,
    ) -> RedeemResult:
        """把邀請碼綁定到某個 Telegram 帳號。"""
        existing = await self.get_user(tg_user_id)
        if existing is not None and existing["status"] == "active":
            return RedeemResult(RedeemStatus.ALREADY_ACTIVE)
        if existing is not None and existing["status"] == "blocked":
            return RedeemResult(RedeemStatus.REVOKED)

        code_hash = hash_code(normalise_code(raw_code))
        invite = await self._db.fetchone(
            "SELECT * FROM invites WHERE code_hash = ?", (code_hash,)
        )
        if invite is None:
            return RedeemResult(RedeemStatus.INVALID)
        if invite["revoked"]:
            return RedeemResult(RedeemStatus.REVOKED)
        if invite["bound_user_id"] is not None and invite["bound_user_id"] != tg_user_id:
            return RedeemResult(RedeemStatus.ALREADY_BOUND)
        if invite["expires_at"]:
            expired = await self._db.fetchval(
                "SELECT ? < datetime('now')", (invite["expires_at"],), default=0
            )
            if expired:
                return RedeemResult(RedeemStatus.EXPIRED)

        async with self._db.transaction() as conn:
            if existing is None:
                await conn.execute(
                    "INSERT INTO users (tg_user_id, status, display_name, username, "
                    "created_at, last_seen_at) VALUES (?, 'active', ?, ?, ?, ?)",
                    (tg_user_id, display_name, username, now_iso(), now_iso()),
                )
            else:
                await conn.execute(
                    "UPDATE users SET status = 'active', display_name = COALESCE(?, display_name), "
                    "username = COALESCE(?, username), last_seen_at = ? WHERE tg_user_id = ?",
                    (display_name, username, now_iso(), tg_user_id),
                )
            await conn.execute(
                "UPDATE invites SET bound_user_id = ?, bound_at = ? WHERE id = ?",
                (tg_user_id, now_iso(), invite["id"]),
            )

        logger.info("邀請碼已綁定：user=%s（%s）", tg_user_id, invite["label"] or "無備註")
        return RedeemResult(RedeemStatus.OK, invite["label"])

    # ── 群組白名單 ──────────────────────────────────────

    async def register_group(self, chat_id: int, title: str | None, added_by: int | None) -> bool:
        """bot 被加入群組時登記。回傳該群組是否已獲授權。"""
        row = await self._db.fetchone("SELECT * FROM groups WHERE chat_id = ?", (chat_id,))
        if row is None:
            await self._db.execute(
                "INSERT INTO groups (chat_id, title, allowed, added_by, created_at) "
                "VALUES (?, ?, 0, ?, ?)",
                (chat_id, title, added_by, now_iso()),
            )
            logger.warning("bot 被加入未授權群組：%s（%s）", chat_id, title)
            return False
        await self._db.execute(
            "UPDATE groups SET title = ? WHERE chat_id = ?", (title, chat_id)
        )
        return bool(row["allowed"])

    async def is_group_allowed(self, chat_id: int) -> bool:
        allowed = await self._db.fetchval(
            "SELECT allowed FROM groups WHERE chat_id = ?", (chat_id,), default=0
        )
        return bool(allowed)

    async def set_group_allowed(self, chat_id: int, allowed: bool) -> bool:
        if await self._db.fetchval(
            "SELECT 1 FROM groups WHERE chat_id = ?", (chat_id,), default=None
        ) is None:
            return False
        await self._db.execute(
            "UPDATE groups SET allowed = ? WHERE chat_id = ?", (1 if allowed else 0, chat_id)
        )
        return True

    async def list_groups(self) -> list:
        return await self._db.fetchall("SELECT * FROM groups ORDER BY created_at DESC")
