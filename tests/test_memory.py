"""自動記憶的行為。

模型輸出不可控，所以解析必須寬容也要保守 —— 寧可漏記，不要把垃圾寫進長期筆記。
"""

from __future__ import annotations

from dafeijing.core.chain import normalise_name
from dafeijing.core.memory import _parse, _parse_attributed, _worth_extracting


def test_parse_plain_lines():
    assert _parse("使用者叫小明\n住在台北", []) == ["使用者叫小明", "住在台北"]


def test_parse_strips_list_markers():
    assert _parse("- 使用者叫小明", []) == ["使用者叫小明"]
    assert _parse("• 使用者叫小明", []) == ["使用者叫小明"]
    assert _parse("* 使用者叫小明", []) == ["使用者叫小明"]
    assert _parse("1. 使用者叫小明", []) == ["使用者叫小明"]
    assert _parse("2) 使用者叫小明", []) == ["使用者叫小明"]


def test_parse_keeps_leading_numbers_that_are_part_of_the_fact():
    # 「3 月要交論文」的 3 是內容，不是編號，不能砍掉
    assert _parse("3 月要交論文", []) == ["3 月要交論文"]
    assert _parse("2026 年開始學 Rust", []) == ["2026 年開始學 Rust"]


def test_parse_recognises_nothing_to_remember():
    assert _parse("無", []) == []
    assert _parse("无", []) == []
    assert _parse("（無）", []) == []
    assert _parse("无。", []) == []
    assert _parse("", []) == []


def test_parse_skips_known_facts():
    assert _parse("- 使用者叫小明", ["使用者叫小明"]) == []


def test_parse_skips_duplicates_within_output():
    assert _parse("小明\n小明", []) == ["小明"]


def test_parse_caps_at_three_by_default():
    raw = "\n".join(f"事實 {index}" for index in range(10))
    assert len(_parse(raw, [])) == 3


def test_parse_limit_is_configurable_for_consolidation():
    raw = "\n".join(f"事實 {index}" for index in range(30))
    assert len(_parse(raw, [], limit=12)) == 12


def test_parse_rejects_overly_long_lines():
    assert _parse("字" * 300, []) == []


def test_parse_handles_indented_output():
    assert _parse("  - 使用者叫小明  ", []) == ["使用者叫小明"]


def test_worth_extracting_skips_small_talk():
    assert not _worth_extracting("嗨")
    assert not _worth_extracting("在嗎")
    assert not _worth_extracting("   ")


def test_worth_extracting_skips_bare_media_markers():
    assert not _worth_extracting("〔貼圖〕")
    assert not _worth_extracting("〔動態貼圖〕")
    assert not _worth_extracting("〔圖片〕")


def test_worth_extracting_accepts_substantive_text():
    assert _worth_extracting("我在做一個 Telegram bot，用 DeepSeek 當模型")
    # 圖片加上說明文字就值得抽取
    assert _worth_extracting("這是我家的貓，牠叫阿肥\n〔圖片〕")


# ── 群組：事實要歸給正確的人 ────────────────────────────

BY_NAME = {normalise_name("甲"): 100, normalise_name("乙"): 200, normalise_name("Ken"): 300}


def test_attributed_parsing_assigns_to_the_right_person():
    raw = "甲｜正在學 Rust\n乙｜做後端，公司在台北"
    facts = _parse_attributed(raw, BY_NAME, {})

    assert (100, "正在學 Rust") in facts
    assert (200, "做後端，公司在台北") in facts


def test_attributed_accepts_halfwidth_pipe():
    assert _parse_attributed("甲|喜歡貓", BY_NAME, {}) == [(100, "喜歡貓")]


def test_attributed_strips_list_markers():
    assert _parse_attributed("- 甲｜喜歡貓", BY_NAME, {}) == [(100, "喜歡貓")]
    assert _parse_attributed("1. 甲｜喜歡貓", BY_NAME, {}) == [(100, "喜歡貓")]


def test_attributed_name_matching_ignores_case_and_spacing():
    assert _parse_attributed("ken｜用 macOS", BY_NAME, {}) == [(300, "用 macOS")]


def test_attributed_drops_unknown_names():
    """名字不在名單上就整行丟掉。

    把關於甲的事記到乙頭上，比漏記更糟 —— 之後會用錯誤的記憶去回應。
    """
    assert _parse_attributed("丙｜完全不在名單上", BY_NAME, {}) == []


def test_attributed_drops_lines_without_a_name():
    # 沒有分隔符的行無法歸屬，不能用「大概是講話的人」來猜
    assert _parse_attributed("正在學 Rust", BY_NAME, {}) == []


def test_attributed_skips_known_facts():
    existing = {100: ["正在學 Rust"]}
    assert _parse_attributed("甲｜正在學 Rust", BY_NAME, existing) == []


def test_attributed_recognises_nothing_to_remember():
    assert _parse_attributed("無", BY_NAME, {}) == []
    assert _parse_attributed("（無）", BY_NAME, {}) == []


def test_attributed_caps_results():
    raw = "\n".join(f"甲｜事實 {i}" for i in range(10))
    assert len(_parse_attributed(raw, BY_NAME, {})) == 3


def test_attributed_does_not_merge_two_people():
    """同一段文字裡對兩個人的描述要分開，不能互相污染。"""
    raw = "甲｜怕辣\n乙｜嗜辣"
    facts = dict(_parse_attributed(raw, BY_NAME, {}))
    assert facts[100] == "怕辣"
    assert facts[200] == "嗜辣"
