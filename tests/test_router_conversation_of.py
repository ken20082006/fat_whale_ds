"""記住本鯨每則回覆屬於邊條對話 —— 「同一串」規則嘅基礎。

**規則（用戶定死）：只有引用本鯨嘅回答先算同一串。**

所以唔使追成條引用鏈：本鯨回覆嗰時將對話名寫入 `group_cache.conversation`，
別人引用本鯨嗰則時查返出嚟就得。引用其他人（用戶3 引用用戶2）查唔到值，
呼叫端就當佢開新串。
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from dafeijing.core.chain import ReplyChain
from dafeijing.store.db import Database

CHAT = -1001324180809
BOT_ID = 999
OTHER_CHAT = -1009999999999


class FakeCfg:
    group_chain_max_messages = 40
    group_chain_max_tokens = 12_000
    group_cache_retention_hours = 72


async def _chain(rows: list[tuple[int, int | None, str | None]]):
    """rows = (message_id, reply_to_id, conversation)"""
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    db = Database(Path(tmp.name) / "test.db")
    await db.connect()
    chain = ReplyChain(db, FakeCfg())
    for message_id, reply_to, conversation in rows:
        await chain.cache_message(
            chat_id=CHAT,
            message_id=message_id,
            reply_to_id=reply_to,
            user_id=BOT_ID if conversation else 111,
            display_name="大肥鯨" if conversation else "甲",
            text=f"msg {message_id}",
            conversation=conversation,
        )
    return chain, tmp


def _lookup(rows, message_id: int, chat_id: int = CHAT):
    async def scenario():
        chain, tmp = await _chain(rows)
        try:
            return await chain.conversation_of(chat_id, message_id)
        finally:
            await chain._db.close()  # noqa: SLF001
            tmp.cleanup()

    return asyncio.run(scenario())


# ── 本鯨嘅回覆 ──────────────────────────────────────────


def test_bot_reply_remembers_its_conversation():
    rows = [(1, None, "grp:x:1"), (2, 1, "grp:x:1")]
    assert _lookup(rows, 2) == "grp:x:1"


def test_non_bot_message_has_no_conversation():
    """普通人發嘅訊息唔會記對話名 —— 引用佢唔算同一串。"""
    rows = [(1, None, None), (2, 1, "grp:x:1")]
    assert _lookup(rows, 1) is None


def test_unknown_message_returns_none():
    assert _lookup([], 500) is None


def test_other_chats_do_not_leak():
    """另一個群有同一個 message_id 係完全可能嘅。"""
    rows = [(1, None, None), (2, 1, "grp:x:1")]
    assert _lookup(rows, 2, chat_id=OTHER_CHAT) is None


# ── 唔可以被覆蓋 ────────────────────────────────────────


def test_conversation_survives_a_later_write_without_it():
    """本鯨自己嗰則之後會經 cache_from_update() 再寫一次（冇 conversation）。
    覆蓋咗就接唔返條對話 —— 同 reply_to_id 嗰個 COALESCE 一樣嘅道理。"""

    async def scenario():
        chain, tmp = await _chain([(2, 1, "grp:x:1")])
        try:
            # 模擬 Telegram 更新帶上嚟嘅同一則（冇 conversation 資訊）
            await chain.cache_message(
                chat_id=CHAT,
                message_id=2,
                reply_to_id=None,
                user_id=BOT_ID,
                display_name="大肥鯨",
                text="msg 2",
            )
            return await chain.conversation_of(CHAT, 2)
        finally:
            await chain._db.close()  # noqa: SLF001
            tmp.cleanup()

    assert asyncio.run(scenario()) == "grp:x:1"


def test_reply_to_id_also_survives():
    """順帶釘住原本嗰個修正：一段對話唔可以爆成 115 個 session。"""

    async def scenario():
        chain, tmp = await _chain([(2, 1, "grp:x:1")])
        try:
            await chain.cache_message(
                chat_id=CHAT,
                message_id=2,
                reply_to_id=None,  # Telegram 更新只帶一層，呢度會係 None
                user_id=BOT_ID,
                display_name="大肥鯨",
                text="msg 2",
            )
            row = await chain._db.fetchone(  # noqa: SLF001
                "SELECT reply_to_id FROM group_cache WHERE chat_id = ? AND message_id = ?",
                (CHAT, 2),
            )
            return row["reply_to_id"]
        finally:
            await chain._db.close()  # noqa: SLF001
            tmp.cleanup()

    assert asyncio.run(scenario()) == 1