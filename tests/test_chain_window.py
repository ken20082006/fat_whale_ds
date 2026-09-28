"""`ReplyChain.window()` —— 聊天室模式嘅近況窗口。

真 DB（tempfile），範式跟 `test_router_conversation_of.py`。

窗口係「觸發嗰則之前、cursor 之後嘅最近 N 則」。三個位最容易寫錯：
1. 包埋觸發嗰則自己（應該唔包）
2. cursor 之後嘅條件寫漏（會重複送已經送過嘅訊息）
3. 本鯨自己嘅訊息冇隔走（會同 Hermes 嘅 assistant turn 撞）
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from dafeijing.core.chain import ReplyChain
from dafeijing.store.db import Database

CHAT = -1001324180809
OTHER_CHAT = -1009999999999
BOT_ID = 999
HUMAN = 111


class FakeCfg:
    group_chain_max_messages = 40
    group_chain_max_tokens = 12_000
    group_cache_retention_hours = 72


async def _chain(rows: list[tuple[int, int, str]]):
    """rows = (message_id, user_id, text)"""
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    db = Database(Path(tmp.name) / "test.db")
    await db.connect()
    chain = ReplyChain(db, FakeCfg())
    for message_id, user_id, text in rows:
        await chain.cache_message(
            chat_id=CHAT,
            message_id=message_id,
            reply_to_id=None,
            user_id=user_id,
            display_name="大肥鯨" if user_id == BOT_ID else "甲",
            text=text,
        )
    return chain, tmp, db


def _run(scenario, rows):
    async def wrapper():
        chain, tmp, db = await _chain(rows)
        try:
            return await scenario(chain)
        finally:
            await db.close()
            tmp.cleanup()

    return asyncio.run(wrapper())


# ── 基本 ────────────────────────────────────────────────


def test_no_cursor_gives_everything_oldest_first():
    """冇 cursor（第一次／reset 後）→ 由舊到新。"""
    rows = [(100 + i, HUMAN, f"msg {100 + i}") for i in range(5)]

    async def scenario(chain):
        entries = await chain.window(CHAT, before_message_id=999, limit=5)
        return [e["message_id"] for e in entries]

    assert _run(scenario, rows) == [100, 101, 102, 103, 104]


def test_window_stops_at_the_trigger():
    """窗口係「觸發之前」—— 由觸發嗰則倒數返上去。"""
    rows = [(100 + i, HUMAN, f"msg {100 + i}") for i in range(5)]

    async def scenario(chain):
        entries = await chain.window(CHAT, before_message_id=103, limit=5)
        return [e["message_id"] for e in entries]

    assert _run(scenario, rows) == [100, 101, 102]


def test_before_message_id_excludes_the_trigger_itself():
    """觸發嗰則會另外送，窗口唔可以包佢 —— 否則同一句出現兩次。"""
    rows = [(100 + i, HUMAN, f"msg {100 + i}") for i in range(5)]

    async def scenario(chain):
        entries = await chain.window(CHAT, before_message_id=104, limit=10)
        return [e["message_id"] for e in entries]

    assert 104 not in _run(scenario, rows)


def test_cursor_returns_only_newer_messages():
    """⭐ cursor 之後嘅新訊息 —— 呢個就係「唔好重複送」嘅全部。"""
    rows = [(100 + i, HUMAN, f"msg {100 + i}") for i in range(6)]

    async def scenario(chain):
        entries = await chain.window(
            CHAT, before_message_id=999, after_message_id=102, limit=10
        )
        return [e["message_id"] for e in entries]

    assert _run(scenario, rows) == [103, 104, 105]


def test_cursor_at_the_end_gives_nothing():
    """啱啱送完，冇新訊息 —— 唔應該回任何嘢（除咗可能冇）。"""
    rows = [(100 + i, HUMAN, f"msg {100 + i}") for i in range(3)]

    async def scenario(chain):
        return await chain.window(
            CHAT, before_message_id=999, after_message_id=102, limit=10
        )

    assert _run(scenario, rows) == []


# ── 隔走本鯨自己 ────────────────────────────────────────


def test_exclude_user_id_drops_the_bot_own_messages():
    rows = [
        (100, HUMAN, "人講嘅"),
        (101, BOT_ID, "本鯨講嘅"),
        (102, HUMAN, "又人講嘅"),
    ]

    async def scenario(chain):
        entries = await chain.window(
            CHAT, before_message_id=999, limit=10, exclude_user_id=BOT_ID
        )
        return [e["message_id"] for e in entries]

    assert _run(scenario, rows) == [100, 102]


def test_without_exclude_the_bot_messages_stay():
    """傳 None 就唔隔（保持函式通用，唔好偷偷隔）。"""
    rows = [(100, HUMAN, "人"), (101, BOT_ID, "鯨")]

    async def scenario(chain):
        entries = await chain.window(CHAT, before_message_id=999, limit=10)
        return [e["message_id"] for e in entries]

    assert _run(scenario, rows) == [100, 101]


# ── 爆上限 ──────────────────────────────────────────────


def test_overflow_keeps_the_newest_and_marks_the_gap():
    """⭐ 用戶明確要嘅：爆上限就只留最新嗰批，前面講明冇咗幾多則。

    呢個就係「唔用純 delta」嘅原因 —— 隔太耐嗰陣 delta 會無限大。
    """
    rows = [(100 + i, HUMAN, f"msg {100 + i}") for i in range(10)]

    async def scenario(chain):
        entries = await chain.window(CHAT, before_message_id=999, limit=3)
        return entries

    entries = _run(scenario, rows)
    assert entries[0]["message_id"] == -1, "最前面要有省略標記"
    assert "7" in entries[0]["text"], "要講明漏咗幾多則"
    assert [e["message_id"] for e in entries[1:]] == [107, 108, 109]


def test_overflow_with_cursor_counts_from_the_cursor():
    """省略嘅數目要由 cursor 起計，唔係由開天闢地起計。"""
    rows = [(100 + i, HUMAN, f"msg {100 + i}") for i in range(10)]

    async def scenario(chain):
        entries = await chain.window(
            CHAT, before_message_id=999, after_message_id=103, limit=2
        )
        return entries

    entries = _run(scenario, rows)
    assert entries[0]["message_id"] == -1
    # cursor 103 之後係 104–109 共 6 則，留最新 2 則（108、109）→ 漏 4 則。
    assert "4" in entries[0]["text"], entries[0]["text"]
    assert [e["message_id"] for e in entries[1:]] == [108, 109]


def test_exactly_at_limit_has_no_marker():
    rows = [(100 + i, HUMAN, f"msg {100 + i}") for i in range(3)]

    async def scenario(chain):
        return await chain.window(CHAT, before_message_id=999, limit=3)

    assert [e["message_id"] for e in _run(scenario, rows)] == [100, 101, 102]


# ── 其他 ────────────────────────────────────────────────


def test_other_chats_are_not_included():
    rows = [(100 + i, HUMAN, f"msg {100 + i}") for i in range(3)]

    async def scenario(chain):
        await chain.cache_message(
            chat_id=OTHER_CHAT,
            message_id=500,
            reply_to_id=None,
            user_id=HUMAN,
            display_name="乙",
            text="第二個群嘅嘢",
        )
        entries = await chain.window(CHAT, before_message_id=999, limit=10)
        return [e["message_id"] for e in entries]

    assert _run(scenario, rows) == [100, 101, 102]


def test_token_budget_falls_back_to_trim():
    """窗口太長就交 `_trim()` 摺疊中段（同引用串共用嗰個機制）。"""
    rows = [(100 + i, HUMAN, "字" * 40) for i in range(10)]

    async def scenario(chain):
        entries = await chain.window(CHAT, before_message_id=999, limit=10, budget=20)
        return entries

    entries = _run(scenario, rows)
    assert any("中間省略" in (e.get("text") or "") for e in entries), (
        f"應該有 _trim 嘅省略標記，實際：{[e.get('text') for e in entries]}"
    )


def test_empty_cache_gives_empty():
    async def scenario(chain):
        return await chain.window(CHAT, before_message_id=999, limit=5)

    assert _run(scenario, []) == []
