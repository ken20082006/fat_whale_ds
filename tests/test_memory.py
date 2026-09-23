"""自動記憶的行為。

模型輸出不可控，所以解析必須寬容也要保守 —— 寧可漏記，不要把垃圾寫進長期筆記。
"""

from __future__ import annotations

from dafeijing.core.memory import _parse, _worth_extracting


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


def test_parse_caps_at_three():
    raw = "\n".join(f"事實 {index}" for index in range(10))
    assert len(_parse(raw, [])) == 3


def test_parse_rejects_overly_long_lines():
    assert _parse("字" * 300, []) == []


def test_parse_handles_indented_output():
    assert _parse("  - 使用者叫小明  ", []) == ["使用者叫小明"]


def test_worth_extracting_skips_small_talk():
    assert not _worth_extracting("嗨")
    assert not _worth_extracting("在嗎")
    assert not _worth_extracting("   ")


def test_worth_extracting_skips_bare_media_markers():
    assert not _worth_extracting("〔貼圖，emoji：😭〕")
    assert not _worth_extracting("〔圖片〕")


def test_worth_extracting_accepts_substantive_text():
    assert _worth_extracting("我在做一個 Telegram bot，用 DeepSeek 當模型")
    # 圖片加上說明文字就值得抽取
    assert _worth_extracting("這是我家的貓，牠叫阿肥\n〔圖片〕")
