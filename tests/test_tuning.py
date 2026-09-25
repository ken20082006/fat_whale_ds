"""執行期調參（/tune）。

只測純邏輯與資料層；Telegram 那層靠實跑。
"""

import asyncio
import tempfile
from pathlib import Path

import pytest

from dafeijing.core.tuning import KNOBS, Tuning, coerce
from dafeijing.settings import MAX_SEARCH_RESULTS, Settings
from dafeijing.store.db import Database


def _cfg() -> Settings:
    return Settings(
        telegram_bot_token="x", openrouter_api_key="y", search_mode="auto"
    )


def test_every_knob_points_at_a_real_field():
    """清單寫錯欄位名，/tune 就會在執行時爆 —— 這裡先擋。"""
    fields = Settings.model_fields
    for knob in KNOBS:
        assert knob.field in fields, knob.field
        # 型別要對得上，否則 setattr 會靜靜塞錯型別
        got = fields[knob.field].annotation
        assert got is knob.kind, f"{knob.field}: {got} != {knob.kind}"


def test_knob_keys_are_unique():
    keys = [knob.key for knob in KNOBS]
    assert len(keys) == len(set(keys))


def test_coerce_validates_choices_and_types():
    mode = next(k for k in KNOBS if k.key == "mode")
    assert coerce(mode, "always") == "always"
    with pytest.raises(ValueError):
        coerce(mode, "triger")          # 打錯字要報錯，不要靜靜接受

    results = next(k for k in KNOBS if k.key == "results")
    assert coerce(results, "7") == 7
    with pytest.raises(ValueError):
        coerce(results, "好多")


def test_coerce_handles_booleans():
    """開關項不可以直接 `bool(value)` —— 那樣 "off" 會變成 True。

    那是最壞的一種錯：使用者以為關咗，其實開咗，而且冇任何提示。
    """
    why = next(k for k in KNOBS if k.key == "why")
    for token in ("on", "ON", "true", "1", "開"):
        assert coerce(why, token) is True, token
    for token in ("off", "OFF", "false", "0", "關"):
        assert coerce(why, token) is False, token
    with pytest.raises(ValueError):
        coerce(why, "開關")          # 兩頭唔到岸要報錯，不要靜靜揀一個


def test_boolean_knob_round_trips_through_the_database():
    """存進資料庫的是 str(True)，重新載入時要解得返。"""
    cfg = _cfg()

    async def scenario() -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Database(Path(tmp) / "t.db")
            await db.connect()
            tuning = Tuning(cfg, db)

            assert cfg.show_reasoning is False        # 預設關（beta）
            await tuning.set("why", "on")
            assert cfg.show_reasoning is True

            fresh = _cfg()
            again = Tuning(fresh, db)
            assert await again.load() == 1
            assert fresh.show_reasoning is True       # 重啟之後仍然開著

            await db.close()

    asyncio.run(scenario())


def test_empty_value_can_be_set():
    """說明寫「留空用引擎預設」，指令就要真係做得到。

    Telegram 的指令參數永遠不會是空字串（空白會被切掉），所以要用 `-`
    這類代表空的寫法。以前沒有這個 —— 說明講得到、做唔到。
    """
    engine_mode = next(k for k in KNOBS if k.key == "engine_mode")
    assert coerce(engine_mode, "-") == ""
    assert coerce(engine_mode, "none") == ""
    assert coerce(engine_mode, "空") == ""
    assert coerce(engine_mode, "fast") == "fast"

    # 只有 str 型別才接受這個寫法 —— 數值項填 `-` 應該報錯而不是變 0
    results = next(k for k in KNOBS if k.key == "results")
    with pytest.raises(ValueError):
        coerce(results, "-")


def test_numeric_knobs_reject_values_outside_the_range():
    """上下限都要擋，唔可以只靠「說明」講。

    實際撞過：`/tune set results_thorough 30` → 之後**每一次**搜尋都 HTTP 400
    （伺服器端工具的 max_results 上限係 25，而 Perplexity 只到 20）。
    錯誤延後到下一次請求才爆，而且完全指唔返係邊個設定搞出嚟。
    """
    thorough = next(k for k in KNOBS if k.key == "results_thorough")
    assert coerce(thorough, str(MAX_SEARCH_RESULTS)) == MAX_SEARCH_RESULTS
    with pytest.raises(ValueError):
        coerce(thorough, str(MAX_SEARCH_RESULTS + 1))
    with pytest.raises(ValueError):
        coerce(thorough, "0")


def test_range_is_shown_in_the_listing():
    """有範圍就要顯示出嚟 —— 否則用家只會由 400 得知上限。"""
    thorough = next(k for k in KNOBS if k.key == "results_thorough")
    assert thorough.range_hint == f"（1–{MAX_SEARCH_RESULTS}）"

    # 沒有範圍的項不要顯示多餘的括號
    context = next(k for k in KNOBS if k.key == "context")
    assert context.range_hint == ""


def test_out_of_range_value_in_the_database_is_ignored():
    """已經寫入資料庫的壞值，唔可以令 bot 起唔到，亦唔可以令搜尋一路 400。

    載入時會被擋下、退回 .env 的值（與下面那個「亂寫的」同一個機制）。
    這一條就是實際那次事故的復原路徑。
    """
    cfg = _cfg()

    async def scenario() -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Database(Path(tmp) / "t.db")
            await db.connect()
            await db.execute(
                "INSERT INTO runtime_settings (key, value, updated_at) VALUES (?,?,?)",
                ("results_thorough", "30", "2026-01-01 00:00:00"),
            )
            tuning = Tuning(cfg, db)
            assert await tuning.load() == 0
            assert cfg.search_results_thorough == 10   # .env 的值
            await db.close()

    asyncio.run(scenario())


def test_set_then_load_reapplies_the_override():
    cfg = _cfg()

    async def scenario() -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Database(Path(tmp) / "t.db")
            await db.connect()
            tuning = Tuning(cfg, db)

            assert await tuning.load() == 0          # 一開始沒有覆寫
            assert cfg.search_mode == "auto"

            old, new = await tuning.set("mode", "always")
            assert (old, new) == ("auto", "always")
            assert cfg.search_mode == "always"       # 立即生效

            # 模擬重啟：新的 cfg 疊上資料庫的覆寫
            fresh = _cfg()
            again = Tuning(fresh, db)
            assert await again.load() == 1
            assert fresh.search_mode == "always"

            await db.close()

    asyncio.run(scenario())


def test_reset_restores_the_env_value():
    cfg = _cfg()

    async def scenario() -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Database(Path(tmp) / "t.db")
            await db.connect()
            tuning = Tuning(cfg, db)

            await tuning.set("mode", "off")
            assert cfg.search_mode == "off"

            old, new = await tuning.reset("mode")
            assert (old, new) == ("off", "auto")     # 還原成 .env 的值
            assert cfg.search_mode == "auto"

            # 再 reset 一次：本來就沒覆寫，回 None
            assert await tuning.reset("mode") is None
            assert await tuning.overridden() == set()

            await db.close()

    asyncio.run(scenario())


def test_broken_value_in_the_database_is_ignored():
    """手改過資料庫、或者舊版留下的不合法值，不可以令 bot 起不來。"""
    cfg = _cfg()

    async def scenario() -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Database(Path(tmp) / "t.db")
            await db.connect()
            await db.execute(
                "INSERT INTO runtime_settings (key, value, updated_at) VALUES (?,?,?)",
                ("mode", "亂寫的", "2026-01-01 00:00:00"),
            )
            await db.execute(
                "INSERT INTO runtime_settings (key, value, updated_at) VALUES (?,?,?)",
                ("已經不在清單裡", "x", "2026-01-01 00:00:00"),
            )

            tuning = Tuning(cfg, db)
            assert await tuning.load() == 0
            assert cfg.search_mode == "auto"          # 用 .env 的值

            await db.close()

    asyncio.run(scenario())


def test_unknown_key_raises():
    cfg = _cfg()

    async def scenario() -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Database(Path(tmp) / "t.db")
            await db.connect()
            tuning = Tuning(cfg, db)
            with pytest.raises(ValueError):
                await tuning.set("冇呢個", "1")
            with pytest.raises(ValueError):
                await tuning.reset("冇呢個")
            await db.close()

    asyncio.run(scenario())
