"""貼圖標記的解析與貼圖庫。"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from dafeijing.core.persona import Persona, PersonaContext
from dafeijing.core.stickers import StickerLibrary, extract_marker
from dafeijing.store.db import Database


def test_extract_marker_basic():
    text, index = extract_marker("講完了。[[貼圖:152]]")
    assert text == "講完了。"
    assert index == 152


def test_extract_marker_tolerates_spacing_and_fullwidth_colon():
    for raw in ("[[貼圖：42]]", "[[ 貼圖 : 42 ]]", "[[貼圖: 42]]"):
        text, index = extract_marker(f"好喔{raw}")
        assert index == 42, raw
        assert text == "好喔"


def test_extract_marker_absent():
    text, index = extract_marker("就是一段普通的回覆")
    assert text == "就是一段普通的回覆"
    assert index is None


def test_extract_marker_removes_trailing_blank_lines():
    text, index = extract_marker("回覆內容\n\n\n[[貼圖:7]]")
    assert index == 7
    assert text == "回覆內容"
    assert "\n\n\n" not in text


def test_extract_marker_keeps_other_text():
    text, index = extract_marker("前言 [[貼圖:1]] 後語")
    assert index == 1
    assert "前言" in text and "後語" in text
    assert "[[" not in text


class FakeCfg:
    pass


def _seed(db_path: Path) -> None:
    conn = __import__("sqlite3").connect(db_path)
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS stickers ("
        "file_unique_id TEXT PRIMARY KEY, set_name TEXT, file_id TEXT, "
        "emoji TEXT, meaning TEXT, usage_hint TEXT, "
        "featured INTEGER NOT NULL DEFAULT 0);"
    )
    conn.executemany(
        "INSERT INTO stickers (file_unique_id, file_id, meaning, usage_hint, featured) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("u1", "f1", "開心歡呼", "想表達開心時", 1),
            ("u2", "f2", "委屈流淚", "想撒嬌求安慰時", 1),
            ("u3", "f3", "沒被選中", "不該出現", 0),
            ("u4", None, "沒有 file_id", "不該出現", 1),
        ],
    )
    conn.commit()
    conn.close()


def test_library_only_loads_featured_with_file_id():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "t.db"
            _seed(path)

            db = Database(path)
            await db.connect()
            library = StickerLibrary(FakeCfg())
            await library.load(db)

            assert library.available
            assert library.resolve(1) is not None
            assert library.resolve(3) is None  # 未精選
            assert library.resolve(4) is None  # 沒有 file_id，送不出去
            assert library.resolve(999) is None

            menu = library.menu()
            assert "1｜" in menu
            assert "2｜" in menu
            assert "不該出現" not in menu

            await db.close()

    asyncio.run(scenario())


def test_library_is_unavailable_when_empty():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "t.db"
            conn = __import__("sqlite3").connect(path)
            conn.executescript(
                "CREATE TABLE IF NOT EXISTS stickers ("
                "file_unique_id TEXT PRIMARY KEY, set_name TEXT, file_id TEXT, "
                "emoji TEXT, meaning TEXT, usage_hint TEXT, "
                "featured INTEGER NOT NULL DEFAULT 0);"
            )
            conn.commit()
            conn.close()

            db = Database(path)
            await db.connect()
            library = StickerLibrary(FakeCfg())
            await library.load(db)

            assert not library.available
            assert library.menu() == ""

            await db.close()

    asyncio.run(scenario())


def test_persona_includes_sticker_menu_when_given():
    prompt = Persona("角色").build(
        PersonaContext(sticker_menu="  1｜想表達開心時\n  2｜想撒嬌時")
    )
    assert "### 貼圖" in prompt
    assert "[[貼圖:編號]]" in prompt
    assert "1｜想表達開心時" in prompt
    assert "偶爾用就好" in prompt


def test_persona_omits_sticker_section_without_menu():
    prompt = Persona("角色").build(PersonaContext())
    assert "### 貼圖" not in prompt
