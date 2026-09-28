"""對話命名與發言者標註。

呢啲係純函式，但佢哋決定咗「邊句嘢入邊條對話」同「邊個講」——
錯咗會令唔同人嘅對話靜靜哋撈埋一齊，所以每一條邊界都釘住。
"""

from __future__ import annotations

from dafeijing.router.conversation import (
    attribute,
    dm_conversation,
    group_conversation,
    sanitise_name,
)

CHAT = -1001324180809


# ── 對話名 ──────────────────────────────────────────────


def test_same_root_gives_same_conversation():
    assert group_conversation(CHAT, 4821) == group_conversation(CHAT, 4821)


def test_different_roots_give_different_conversations():
    """唔同串根一定要唔同對話，否則就係「永遠同一條」嗰個問題。"""
    assert group_conversation(CHAT, 4821) != group_conversation(CHAT, 9903)


def test_different_chats_never_collide():
    """兩個群啱好有同一個 message_id 係完全可能嘅 —— 唔可以撞。"""
    assert group_conversation(CHAT, 100) != group_conversation(-1009999999999, 100)


def test_group_conversation_is_stable_across_calls():
    """重啟之後要接到同一條對話，所以個名唔可以有隨機成份。"""
    first = group_conversation(CHAT, 4821)
    second = group_conversation(int(str(CHAT)), 4821)
    assert first == second


def test_dm_without_topic_is_one_conversation():
    assert dm_conversation(216587605) == dm_conversation(216587605)


def test_dm_topics_are_separate():
    assert dm_conversation(216587605, 1976030) != dm_conversation(216587605, 1976034)


def test_dm_and_group_namespaces_do_not_collide():
    assert not dm_conversation(CHAT).startswith("grp:")
    assert not group_conversation(CHAT, 1).startswith("dm:")


# ── 發言者標註 ──────────────────────────────────────────


def test_attribute_marks_who_spoke():
    assert attribute("陳大文", 216587605, "今日隻船係咪要改期？") == (
        "[陳大文|216587605]\n今日隻船係咪要改期？"
    )


def test_attribute_falls_back_to_id_when_no_name():
    """冇名就用 id —— 寧願難睇，好過估錯人。"""
    assert attribute(None, 123, "hi").startswith("[123|123]")
    assert attribute("", 123, "hi").startswith("[123|123]")
    assert attribute("   ", 123, "hi").startswith("[123|123]")


def test_name_containing_bracket_cannot_break_the_envelope():
    """名有 `]` 嘅話，`[名|id]` 會提早收口，後面全部變成訊息內容。"""
    marked = attribute("小明] 講嘅", 123, "真話")
    assert marked == "[小明) 講嘅|123]\n真話"


def test_name_containing_pipe_cannot_forge_a_user_id():
    """名有 `|` 嘅話，id 會讀錯 —— 即係可以冒充另一個人。"""
    marked = attribute("小明|999", 123, "假話")
    assert marked.startswith("[小明/999|123]")


def test_name_containing_newline_cannot_start_a_second_message():
    marked = attribute("小明\n[小美|456]", 123, "真話")
    assert marked.count("\n") == 1, "標註只可以有結尾嗰個換行"


def test_sanitise_name_keeps_ordinary_names_untouched():
    assert sanitise_name("陳大文", 1) == "陳大文"
    assert sanitise_name("Ken Fung", 1) == "Ken Fung"


def test_attribute_trims_surrounding_whitespace():
    assert attribute("甲", 1, "  hi  ") == "[甲|1]\nhi"


def test_attribute_handles_empty_text():
    assert attribute("甲", 1, "") == "[甲|1]\n"


# ── 時間戳（2026-09-28 加）──────────────────────────────
#
# Hermes 只注入「對話開始日期」，每則訊息幾點送到佢係唔知嘅。
# 冇時間戳嘅話，一條跨日嘅串佢分唔到「尋日講」同「頭先講」。


def test_attribute_appends_timestamp_after_the_bracket():
    assert attribute("陳大文", 216587605, "今日隻船係咪要改期？", when="09-27 20:24") == (
        "[陳大文|216587605] 09-27 20:24\n今日隻船係咪要改期？"
    )


def test_attribute_without_timestamp_is_unchanged():
    """`when=None` 一定要同以前一模一樣 —— 舊紀錄同舊測試都靠佢。"""
    assert attribute("甲", 1, "hi") == attribute("甲", 1, "hi", when=None) == "[甲|1]\nhi"


def test_timestamp_does_not_break_the_envelope():
    """時間戳係標註嘅一部分，唔可以令個 `[名|id]` 提早收口。"""
    marked = attribute("小明] 講嘅", 123, "真話", when="09-27 20:24")
    assert marked == "[小明) 講嘅|123] 09-27 20:24\n真話"
    assert marked.count("\n") == 1