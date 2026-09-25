"""對話命名與發言者標註。

呢啲係純函式，但佢哋決定咗「邊句嘢入邊條對話」同「邊個講」——
錯咗會令唔同人嘅對話靜靜哋撈埋一齊，所以每一條邊界都釘住。
"""

from __future__ import annotations

from dafeijing.router.conversation import (
    build_input,
    pending_since_bot,
    attribute,
    dm_conversation,
    group_conversation,
    sanitise_name,
)

CHAT = -1001324180809


# ── 對話名 ──────────────────────────────────────────────


def test_same_chain_root_gives_same_conversation():
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
    """私聊同群組嘅名一定要分得開。"""
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


# ── 只送「上次 bot 發言之後」嘅新訊息 ──────────────────

BOT = 999


def _msg(message_id: int, user_id: int, name: str, text: str) -> dict:
    return {
        "message_id": message_id,
        "user_id": user_id,
        "display_name": name,
        "text": text,
    }


def test_no_history_sends_nothing():
    assert pending_since_bot([], BOT) == []


def test_bot_never_spoke_sends_the_whole_chain():
    """第一次觸發 —— Hermes 嗰邊冇任何歷史，要送晒。"""
    chain = [_msg(1, 111, "甲", "第一句"), _msg(2, 222, "乙", "第二句")]
    assert pending_since_bot(chain, BOT) == chain


def test_nothing_new_after_the_bots_reply():
    """Hermes 已經有齊，唔使再送 —— 否則會見到自己講過嘅嘢再講一次。"""
    chain = [_msg(1, 111, "甲", "問"), _msg(2, BOT, "大肥鯨", "答")]
    assert pending_since_bot(chain, BOT) == []


def test_sends_bystander_messages_the_bot_never_saw():
    """bot 回覆咗 → 乙插嘴（冇 @）→ 甲 @ 本鯨。

    只送甲嗰句嘅話 Hermes 完全唔知乙講過乜，所以連乙嗰句都要送。
    """
    chain = [
        _msg(1, 111, "甲", "問"),
        _msg(2, BOT, "大肥鯨", "答"),
        _msg(3, 222, "乙", "我插嘴"),
        _msg(4, 111, "甲", "咁你覺得點"),
    ]
    pending = pending_since_bot(chain, BOT)

    assert [m["message_id"] for m in pending] == [3, 4]


def test_only_the_latest_bot_turn_counts():
    """bot 講過幾次 —— 由**最後一次**之後開始計。"""
    chain = [
        _msg(1, 111, "甲", "問1"),
        _msg(2, BOT, "大肥鯨", "答1"),
        _msg(3, 111, "甲", "問2"),
        _msg(4, BOT, "大肥鯨", "答2"),
        _msg(5, 111, "甲", "問3"),
    ]
    assert [m["message_id"] for m in pending_since_bot(chain, BOT)] == [5]


def test_bot_id_none_sends_the_whole_chain():
    """未連上 Telegram 之前 bot_id 係 None —— 唔可以當所有人係 bot。"""
    chain = [_msg(1, 111, "甲", "問"), _msg(2, 222, "乙", "答")]
    assert pending_since_bot(chain, None) == chain


# ── 砌送去 Hermes 嘅文字 ────────────────────────────────


FALLBACK = ("甲", 111, "觸發嗰句")


def test_build_input_marks_every_speaker():
    """一條串幾個人 —— 每個都要標，否則 Hermes 當同一個人。"""
    entries = [_msg(3, 222, "乙", "我插嘴"), _msg(4, 111, "甲", "咁你覺得點")]
    assert build_input(entries, FALLBACK) == (
        "[乙|222]\n我插嘴\n\n[甲|111]\n咁你覺得點"
    )


def test_build_input_falls_back_when_chain_is_empty():
    """追唔到串（訊息未入快取）都要有嘢送，唔可以送空。"""
    assert build_input([], FALLBACK) == attribute(*FALLBACK)


def test_build_input_falls_back_when_every_entry_is_blank():
    entries = [_msg(3, 222, "乙", "   "), _msg(4, 111, "甲", "")]
    assert build_input(entries, FALLBACK) == attribute(*FALLBACK)


def test_build_input_skips_blank_entries_but_keeps_the_rest():
    entries = [_msg(3, 222, "乙", "有嘢"), _msg(4, 111, "甲", "  ")]
    assert build_input(entries, FALLBACK) == "[乙|222]\n有嘢"


def test_media_only_message_still_reaches_hermes():
    """純媒體訊息冇文字 —— 但大肥鯨會加一句標註，所以正常情況唔會空。"""
    entries = [{"message_id": 5, "user_id": 222, "display_name": "乙", "text": "〔圖片〕"}]
    assert build_input(entries, FALLBACK) == "[乙|222]\n〔圖片〕"

