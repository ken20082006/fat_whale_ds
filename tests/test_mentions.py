"""@ 提及的解析。

群組的事實要歸給正確的人，關鍵是能不能從訊息裡認出 @ 的是誰。
MENTION 只給帳號名（要再查名冊），TEXT_MENTION 直接帶 user 物件。
"""

from __future__ import annotations

from types import SimpleNamespace

from telegram import MessageEntity

from dafeijing.bot.group import _mentioned_names


def _message(text, entities, caption=None, caption_entities=None):
    return SimpleNamespace(
        text=text,
        entities=entities,
        caption=caption,
        caption_entities=caption_entities,
    )


def _mention(text, handle):
    offset = text.index("@" + handle)
    return MessageEntity(type=MessageEntity.MENTION, offset=offset, length=len(handle) + 1)


def test_extracts_mentioned_handles():
    text = "@alice 你覺得點"
    names = _mentioned_names(_message(text, [_mention(text, "alice")]), "mybot")
    assert names == ["alice"]


def test_skips_the_bot_itself():
    text = "@mybot 你好"
    names = _mentioned_names(_message(text, [_mention(text, "mybot")]), "mybot")
    assert names == []


def test_bot_username_matching_is_case_insensitive():
    text = "@MyBot 你好"
    names = _mentioned_names(_message(text, [_mention(text, "MyBot")]), "mybot")
    assert names == []


def test_multiple_mentions():
    text = "@alice @bob 你地覺得點"
    entities = [_mention(text, "alice"), _mention(text, "bob")]
    assert _mentioned_names(_message(text, entities), "mybot") == ["alice", "bob"]


def test_text_mention_uses_display_name():
    """沒有帳號名的人會以 TEXT_MENTION 出現，直接帶 user 物件。"""
    text = "Ken 你覺得點"
    entity = MessageEntity(
        type=MessageEntity.TEXT_MENTION,
        offset=0,
        length=3,
        user=SimpleNamespace(id=42, full_name="Ken Fung", username=None),
    )
    assert _mentioned_names(_message(text, [entity]), "mybot") == ["Ken Fung"]


def test_reads_caption_entities_for_media():
    caption = "@alice 睇下呢張"
    names = _mentioned_names(
        _message(None, None, caption=caption, caption_entities=[_mention(caption, "alice")]),
        "mybot",
    )
    assert names == ["alice"]


def test_no_entities():
    assert _mentioned_names(_message("普通訊息", None), "mybot") == []


def test_ignores_non_mention_entities():
    text = "http://example.com 普通訊息"
    entity = MessageEntity(type=MessageEntity.URL, offset=0, length=18)
    assert _mentioned_names(_message(text, [entity]), "mybot") == []
