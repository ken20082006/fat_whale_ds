"""設定欄位本身的正確性。

Python 的類別不會阻止同名欄位重複定義 —— 後面的直接覆蓋前面的，而且不會報錯。
這實際發生過一次：`search_mode`（什麼時候搜）與引擎分級用了同一個名字，
兩邊黏在一起，還碰巧能跑。
"""

from __future__ import annotations

from dafeijing.core.webfetch import SEARCH_MODES
from dafeijing.settings import Settings


def test_search_fields_are_distinct():
    fields = Settings.model_fields

    assert "search_mode" in fields, "搜尋積極程度"
    assert "search_engine_mode" in fields, "引擎自身的分級"
    assert "search_engine" in fields

    # 兩者的預設值不該相同，否則很可能又是同一個欄位被覆蓋
    assert fields["search_mode"].default != fields["search_engine_mode"].default


def test_default_search_mode_is_valid():
    assert Settings.model_fields["search_mode"].default in SEARCH_MODES


def test_required_fields_have_no_defaults():
    """必填欄位不該有預設值，否則會用假金鑰靜靜啟動。"""
    for name in ("telegram_bot_token", "openrouter_api_key"):
        assert Settings.model_fields[name].is_required(), name


def test_reasoning_off_by_default():
    """推理 token 以輸出計價，預設必須是關的。"""
    assert Settings.model_fields["reasoning_enabled"].default is False


def test_admin_ids_parsing():
    settings = Settings.model_construct(admin_user_ids="123, 456;789 , abc, 0")
    assert settings.admin_ids == {123, 456, 789, 0}
    assert settings.is_admin(456)
    assert not settings.is_admin(999)
    assert not settings.is_admin(None)


def test_admin_ids_empty():
    settings = Settings.model_construct(admin_user_ids="")
    assert settings.admin_ids == set()
    assert not settings.is_admin(1)
