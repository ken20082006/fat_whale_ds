"""群組概況。

測資料層與純邏輯；真正產生概況那一步要打模型，交給實跑驗證。
"""

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dafeijing.core.groupprofile import GroupProfiler
from dafeijing.core.session import SessionManager
from dafeijing.store.db import Database


class FakeCfg:
    window_turns = 4
    compact_trigger_tokens = 10_000
    summary_max_tokens = 200
    idle_reset_minutes = 120
    group_thread_ttl_minutes = 360
    notes_per_scope_max = 60

    group_profile_enabled = True
    group_profile_min_exchanges = 8
    group_profile_max_exchanges = 60
    group_profile_min_hours = 6.0
    group_profile_max_chars = 600


async def _new_db(tmp: Path) -> Database:
    db = Database(tmp / "test.db")
    await db.connect()
    return db


def _stamp(minutes_ago: int) -> str:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


# ── 素材範圍：只拿機器人親自參與過的 ─────────────────


def test_exchanges_cover_only_this_group():
    """別的群與私聊的訊息都不該混進來 —— 素材限於這個群裡它參與過的交流。"""
    cfg = FakeCfg()

    async def scenario() -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = await _new_db(Path(tmp))
            sessions = SessionManager(db, cfg)

            here = await sessions.group_thread_session(-100, 1)
            elsewhere = await sessions.group_thread_session(-200, 2)
            private = await sessions.private_session(7)

            await sessions.append(here.id, "user", "呢個群係咩群")
            await sessions.append(here.id, "assistant", "係相機谷")
            await sessions.append(elsewhere.id, "user", "唔應該出現")
            await sessions.append(private.id, "user", "私聊都唔應該出現")

            rows = await sessions.group_exchanges_since(-100, None)

            contents = [r["content"] for r in rows]
            assert contents == ["呢個群係咩群", "係相機谷"]
            assert "唔應該出現" not in contents
            assert "私聊都唔應該出現" not in contents

            await db.close()

    asyncio.run(scenario())


def test_exchanges_are_oldest_first():
    cfg = FakeCfg()

    async def scenario() -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = await _new_db(Path(tmp))
            sessions = SessionManager(db, cfg)
            session = await sessions.group_thread_session(-100, 1)

            for index in range(4):
                await sessions.append(session.id, "user", f"第 {index} 句")

            rows = await sessions.group_exchanges_since(-100, None)
            assert [r["content"] for r in rows] == [
                "第 0 句",
                "第 1 句",
                "第 2 句",
                "第 3 句",
            ]

            await db.close()

    asyncio.run(scenario())


def test_exchanges_respect_the_high_water_mark():
    """只讀上次涵蓋時間點之後的 —— 舊的已經併進概況了，不必重讀。"""
    cfg = FakeCfg()

    async def scenario() -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = await _new_db(Path(tmp))
            sessions = SessionManager(db, cfg)
            session = await sessions.group_thread_session(-100, 1)

            await sessions.append(session.id, "user", "很舊的一句")
            await db.execute(
                "UPDATE messages SET created_at = ? WHERE content = '很舊的一句'",
                (_stamp(600),),
            )
            await sessions.append(session.id, "user", "新的一句")

            rows = await sessions.group_exchanges_since(-100, _stamp(60))
            assert [r["content"] for r in rows] == ["新的一句"]

            await db.close()

    asyncio.run(scenario())


# ── 概況的讀寫 ───────────────────────────────────────


def test_group_profile_upsert():
    cfg = FakeCfg()

    async def scenario() -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = await _new_db(Path(tmp))
            sessions = SessionManager(db, cfg)

            assert await sessions.get_group_profile(-100) is None

            await sessions.set_group_profile(-100, "第一版概況", _stamp(60))
            row = await sessions.get_group_profile(-100)
            assert row["content"] == "第一版概況"

            # 第二次要覆蓋，不是新增一列
            await sessions.set_group_profile(-100, "第二版概況", _stamp(10))
            row = await sessions.get_group_profile(-100)
            assert row["content"] == "第二版概況"
            count = await db.fetchval(
                "SELECT COUNT(*) FROM group_profile WHERE chat_id = ?", (-100,)
            )
            assert count == 1

            # 每個群各自一份
            assert await sessions.get_group_profile(-200) is None

            await db.close()

    asyncio.run(scenario())


# ── 更新頻率的閘 ─────────────────────────────────────


def test_profile_is_due_when_missing_or_old():
    profiler = GroupProfiler(FakeCfg(), None, None)

    assert profiler._due(None) is True
    assert profiler._due({"summarized_at": _stamp(600)}) is True
    assert profiler._due({"summarized_at": _stamp(30)}) is False


def test_profile_is_due_again_when_the_gate_is_off():
    """門檻設為 0 就每次都跑 —— 給測試與臨時觀察用。"""
    cfg = FakeCfg()
    cfg.group_profile_min_hours = 0
    profiler = GroupProfiler(cfg, None, None)
    assert profiler._due({"summarized_at": _stamp(1)}) is True


def test_a_broken_timestamp_does_not_block_updates():
    profiler = GroupProfiler(FakeCfg(), None, None)
    assert profiler._due({"summarized_at": "亂寫的"}) is True
