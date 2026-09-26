"""貼圖 —— 清單注入（一個對話一次）同埋 `[[貼圖:編號]]` 嘅處理。

模型唔可能每次睇一百幾十張圖去揀，所以清單係離線標註好嘅精選，
模型用編號指明，router 負責送出。
"""

from __future__ import annotations

from types import SimpleNamespace

from dafeijing.router.stickers import menu_block, split_marker

MENU = "  1｜打招呼\n  2｜無言以對\n  3｜開心"


# ── 清單區塊 ────────────────────────────────────────────


def test_menu_block_wraps_the_list_in_markers():
    block = menu_block(MENU)

    assert "<可用貼圖>" in block and "</可用貼圖>" in block
    assert "1｜打招呼" in block


def test_menu_block_explains_the_marker():
    block = menu_block(MENU)
    assert "[[貼圖:編號]]" in block


def test_menu_block_says_do_not_invent_numbers():
    """模型自己編編號就會送出唔存在嘅貼圖。"""
    block = menu_block(MENU)
    assert "唔好自己編" in block


def test_menu_block_has_frequency_guidance():
    """唔講頻率嘅話，模型會每則都送。"""
    block = menu_block(MENU)
    assert "五到十則" in block
    assert "唔好連續兩則" in block


def test_empty_menu_gives_nothing():
    assert menu_block("") == ""
    assert menu_block("   \n ") == ""


# ── 標記處理 ────────────────────────────────────────────


def test_marker_is_removed_from_the_reply():
    """標記唔可以畀使用者見到。"""
    cleaned, index = split_marker("好呀，本鯨幫你睇下。[[貼圖:3]]")

    assert index == 3
    assert "[[貼圖" not in cleaned
    assert cleaned == "好呀，本鯨幫你睇下。"


def test_no_marker_leaves_text_alone():
    cleaned, index = split_marker("純文字回覆")
    assert cleaned == "純文字回覆"
    assert index is None


def test_full_width_colon_is_tolerated():
    """模型好容易打全角冒號。"""
    _, index = split_marker("好[[貼圖：7]]")
    assert index == 7


def test_spaces_inside_the_marker_are_tolerated():
    _, index = split_marker("好[[ 貼圖 : 12 ]]")
    assert index == 12


def test_marker_removal_does_not_leave_a_pile_of_blank_lines():
    cleaned, index = split_marker("本鯨答你\n\n[[貼圖:1]]")
    assert index == 1
    assert "\n\n\n" not in cleaned
    assert cleaned == "本鯨答你"


def test_first_marker_wins():
    """模型唔應該出兩個，但真係出咗就用第一個。"""
    _, index = split_marker("[[貼圖:1]] 文字 [[貼圖:2]]")
    assert index == 1


# ── 送出 ────────────────────────────────────────────────


def test_resolve_returns_none_for_an_unknown_index():
    """模型編錯號要靜靜哋當冇，唔可以拋錯。"""
    from dafeijing.core.stickers import StickerLibrary

    library = StickerLibrary(SimpleNamespace())
    assert library.resolve(999) is None
    assert not library.available
    assert library.menu() == ""