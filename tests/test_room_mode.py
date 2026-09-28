"""群組「聊天室模式」嘅兩個開關同 cursor。

真 DB（tempfile），範式跟 `test_router_conversation_of.py`。
重點測**兩個開關互不干擾** —— 佢哋係兩條獨立 SQL，寫錯就好容易一個覆蓋另一個。
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from dafeijing.core.room import RoomMode
from dafeijing.store.db import Database

CHAT = -1001324180809
OTHER_CHAT = -1009999999999
CONV = f"room:{CHAT}"


async def _room():
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    db = Database(Path(tmp.name) / "test.db")
    await db.connect()
    return RoomMode(db), tmp, db


def _run(scenario):
    async def wrapper():
        rooms, tmp, db = await _room()
        try:
            return await scenario(rooms)
        finally:
            await db.close()
            tmp.cleanup()

    return asyncio.run(wrapper())


# ── 開關 ────────────────────────────────────────────────


def test_unset_group_is_all_off():
    """未設定過嘅群 —— 兩個都要係 False，唔可以爆。"""

    async def scenario(rooms):
        return await rooms.status(CHAT)

    assert _run(scenario) == (False, False)


def test_room_enabled_roundtrip():
    async def scenario(rooms):
        await rooms.set_room_enabled(CHAT, True)
        return await rooms.room_enabled(CHAT)

    assert _run(scenario) is True


def test_read_background_roundtrip():
    async def scenario(rooms):
        await rooms.set_read_background(CHAT, True)
        return await rooms.read_background(CHAT)

    assert _run(scenario) is True


def test_the_two_switches_do_not_clobber_each_other():
    """⭐ 核心：呢兩個開關係獨立嘅。

    先開 A 再開 B，A 唔可以跌；反過嚟一樣。
    寫成一條 SQL 好容易整爆呢點。
    """

    async def scenario(rooms):
        await rooms.set_room_enabled(CHAT, True)
        await rooms.set_read_background(CHAT, True)
        after_both = await rooms.status(CHAT)

        await rooms.set_room_enabled(CHAT, False)
        room_off_read_on = await rooms.status(CHAT)

        await rooms.set_room_enabled(CHAT, True)
        await rooms.set_read_background(CHAT, False)
        return after_both, room_off_read_on, await rooms.status(CHAT)

    after_both, room_off_read_on, read_off = _run(scenario)
    assert after_both == (True, True)
    assert room_off_read_on == (False, True), "關 A 唔可以連 B 都關埋"
    assert read_off == (True, False), "關 B 唔可以連 A 都關埋"


def test_switches_are_per_group():
    """A 群開咗唔可以影響 B 群。"""

    async def scenario(rooms):
        await rooms.set_room_enabled(CHAT, True)
        return await rooms.status(OTHER_CHAT)

    assert _run(scenario) == (False, False)


# ── cursor ──────────────────────────────────────────────


def test_cursor_starts_none():
    async def scenario(rooms):
        return await rooms.cursor(CONV)

    assert _run(scenario) is None


def test_cursor_roundtrip_and_overwrite():
    async def scenario(rooms):
        await rooms.set_cursor(CONV, 500)
        first = await rooms.cursor(CONV)
        await rooms.set_cursor(CONV, 900)
        return first, await rooms.cursor(CONV)

    assert _run(scenario) == (500, 900)


def test_cursor_is_keyed_by_conversation_not_chat():
    """開關 A 關咗嗰陣，同一個群有好多條對話 —— 每條各自一個 cursor。

    用 chat_id 做 key 嘅話，兩條串會互相搶 cursor，一條睇過嘅訊息
    另一條就唔會再收到。
    """

    async def scenario(rooms):
        other = f"grp:{CHAT}:777"
        await rooms.set_cursor(CONV, 500)
        await rooms.set_cursor(other, 900)
        return await rooms.cursor(CONV), await rooms.cursor(other)

    assert _run(scenario) == (500, 900)


def test_reset_cursor_makes_it_none_again():
    """`/room reset` —— 下次 @ 會補返最近 N 條。"""

    async def scenario(rooms):
        await rooms.set_cursor(CONV, 900)
        await rooms.reset_cursor(CONV)
        return await rooms.cursor(CONV)

    assert _run(scenario) is None


def test_reset_cursor_does_not_touch_switches():
    """/room reset 只清 cursor，唔可以順手熄咗個模式。"""

    async def scenario(rooms):
        await rooms.set_room_enabled(CHAT, True)
        await rooms.set_read_background(CHAT, True)
        await rooms.set_cursor(CONV, 900)
        await rooms.reset_cursor(CONV)
        return await rooms.status(CHAT), await rooms.cursor(CONV)

    assert _run(scenario) == ((True, True), None)


def test_cursor_survives_setting_switches():
    """改開關唔可以清走 cursor（否則會重複送已經送過嘅訊息）。"""

    async def scenario(rooms):
        await rooms.set_cursor(CONV, 900)
        await rooms.set_room_enabled(CHAT, True)
        await rooms.set_read_background(CHAT, True)
        return await rooms.cursor(CONV)

    assert _run(scenario) == 900
