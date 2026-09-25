"""追串根 —— router 用佢做對話名。

**呢個係「每串一個對話」成敗所在**：串根追錯，兩條唔同嘅串就會撞成
同一條對話（變成永遠續同一條），或者同一條串會斷開兩條。

刻意唔用 `ReplyChain.resolve()`：`resolve()` 會為咗餵模型而摺疊中段，
摺疊標記嘅 `message_id` 係 -1。串根啱好被摺走嘅話，`chain[0]` 就係 -1，
所有咁樣嘅串就撞埋一齊。
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from dafeijing.core.chain import ReplyChain
from dafeijing.store.db import Database

CHAT = -1001324180809


class FakeCfg:
    group_chain_max_messages = 40
    group_chain_max_tokens = 12_000
    group_cache_retention_hours = 72


async def _chain_with(rows: list[tuple[int, int | None]]) -> ReplyChain:
    """喺臨時 DB 起一條串。`rows` 係 (message_id, reply_to_id)。"""
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    db = Database(Path(tmp.name) / "test.db")
    await db.connect()
    for message_id, reply_to in rows:
        await db.execute(
            "INSERT INTO group_cache (chat_id, message_id, reply_to_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (CHAT, message_id, reply_to, "2026-01-01T00:00:00+00:00"),
        )
    chain = ReplyChain(db, FakeCfg())
    return chain, tmp  # type: ignore[return-value]


def _root(rows: list[tuple[int, int | None]], leaf: int) -> int:
    async def scenario() -> int:
        chain, tmp = await _chain_with(rows)  # type: ignore[misc]
        try:
            return await chain.root_id(CHAT, leaf)
        finally:
            await chain._db.close()  # noqa: SLF001
            tmp.cleanup()

    return asyncio.run(scenario())


# ── 開新對話 vs 續舊對話 ────────────────────────────────


def test_message_not_in_cache_is_its_own_root():
    """冇引用任何訊息（即係開新串）→ 佢自己就係串根 → 開新對話。"""
    assert _root([], 500) == 500


def test_message_without_reply_is_its_own_root():
    assert _root([(500, None)], 500) == 500


def test_reply_walks_up_to_the_root():
    """A ← B ← C：C 係 leaf，串根係 A。"""
    rows = [(100, None), (200, 100), (300, 200)]
    assert _root(rows, 300) == 100


def test_two_different_chains_do_not_share_a_root():
    """兩條各自獨立嘅串，串根一定要唔同 —— 否則就係「永遠同一條對話」。"""
    rows = [(100, None), (200, 100), (400, None), (500, 400)]
    assert _root(rows, 200) == 100
    assert _root(rows, 500) == 400
    assert _root(rows, 200) != _root(rows, 500)


def test_mid_chain_message_resolves_to_the_same_root():
    """由串中間任何一點追上去，都要追到同一個根。"""
    rows = [(100, None), (200, 100), (300, 200), (400, 300)]
    roots = {_root(rows, leaf) for leaf in (100, 200, 300, 400)}
    assert roots == {100}


def test_reply_to_an_uncached_message_stops_there():
    """引用咗一則冇快取嘅訊息（例如其他 bot 發嘅）—— 停喺嗰度，
    唔好一路追落去，亦唔好當佢係根。"""
    rows = [(500, 999)]  # 999 唔喺快取
    assert _root(rows, 500) == 999


# ── 穩健性 ──────────────────────────────────────────────


def test_cycle_does_not_hang():
    """資料庫萬一有環（理論上唔應該），唔可以死循環。"""
    rows = [(1, 2), (2, 1)]
    assert _root(rows, 1) in (1, 2)


def test_never_returns_the_elision_marker():
    """-1 係 `resolve()` 摺疊中段用嘅標記。串根用佢做對話名嘅話，
    所有被摺疊嘅串都會撞成同一條對話。"""
    rows = [(100, None), (200, 100), (300, 200)]
    for leaf in (100, 200, 300):
        assert _root(rows, leaf) != -1


def test_other_chats_do_not_leak_in():
    """另一個群有同一個 message_id 係完全可能嘅，唔可以撈埋。"""

    async def scenario() -> int:
        chain, tmp = await _chain_with([(100, None), (200, 100)])  # type: ignore[misc]
        try:
            await chain._db.execute(  # noqa: SLF001
                "INSERT INTO group_cache (chat_id, message_id, reply_to_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (-100999, 200, 100, "2026-01-01T00:00:00+00:00"),
            )
            return await chain.root_id(-100999, 200)
        finally:
            await chain._db.close()  # noqa: SLF001
            tmp.cleanup()

    assert asyncio.run(scenario()) == 100
