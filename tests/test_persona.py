from __future__ import annotations

import tempfile
from pathlib import Path

from dafeijing.core.persona import Persona, PersonaContext


def test_media_rule_is_always_present():
    prompt = Persona("你是一隻鯨魚。").build(PersonaContext())
    assert "### 圖片與貼圖" in prompt
    assert "不要描述畫面" in prompt


def test_media_rule_survives_a_completely_different_persona():
    """規則寫在程式而非人設檔，換一份人設也該成立。"""
    prompt = Persona("你是客服助理，只回答產品問題。").build(PersonaContext())
    assert "### 圖片與貼圖" in prompt


def test_media_rule_keeps_the_explicit_request_exception():
    prompt = Persona("你是一隻鯨魚。").build(PersonaContext())
    # 必須保留「對方明確要求看圖時要認真讀」這個例外
    assert "明確要你看圖" in prompt


def test_persona_body_comes_first():
    prompt = Persona("獨一無二的角色開場白。").build(PersonaContext())
    assert prompt.startswith("獨一無二的角色開場白。")


def test_vibe_is_reflected():
    low = Persona("角色").build(PersonaContext(vibe="low"))
    high = Persona("角色").build(PersonaContext(vibe="high"))
    assert "低" in low
    assert "高" in high
    assert low != high


def test_unknown_vibe_falls_back_to_default():
    prompt = Persona("角色").build(PersonaContext(vibe="亂寫的"))
    assert "中" in prompt


def test_group_and_private_differ():
    private = Persona("角色").build(PersonaContext(is_group=False))
    group = Persona("角色").build(PersonaContext(is_group=True))
    assert "私聊" in private
    assert "群組" in group


def test_notes_and_summary_are_included():
    prompt = Persona("角色").build(
        PersonaContext(notes=["使用者叫小明", "住在台北"], summary="先前聊過排版問題")
    )
    assert "使用者叫小明" in prompt
    assert "住在台北" in prompt
    assert "先前聊過排版問題" in prompt


def test_empty_notes_and_summary_are_omitted():
    prompt = Persona("角色").build(PersonaContext())
    assert "長期記憶" not in prompt
    assert "較早對話的摘要" not in prompt


def test_falls_back_to_example_when_missing():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        example = Path(tmp) / "persona.example.md"
        example.write_text("這是範本內容。", encoding="utf-8")
        persona = Persona.load(Path(tmp) / "persona.md")
        assert persona.body == "這是範本內容。"
        assert persona.source == example


def test_falls_back_to_builtin_when_nothing_exists():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        persona = Persona.load(Path(tmp) / "persona.md")
        assert persona.source is None
        assert persona.body
