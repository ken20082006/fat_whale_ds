"""圖片與貼圖的標註與挑選。"""

from __future__ import annotations

from types import SimpleNamespace

from dafeijing.core.media import describe, pick_file


def _sticker(*, animated=False, video=False, emoji="😭", thumbnail="thumb-id"):
    return SimpleNamespace(
        sticker=SimpleNamespace(
            is_animated=animated,
            is_video=video,
            emoji=emoji,
            file_id="sticker-id",
            thumbnail=SimpleNamespace(file_id=thumbnail) if thumbnail else None,
        ),
        photo=None,
        document=None,
    )


def test_sticker_marker_omits_the_emoji():
    """emoji 是貼圖包作者標的，可能與畫面不符，不該餵給模型。"""
    marker = describe(_sticker(emoji="😭"))
    assert marker == "〔貼圖〕"
    assert "😭" not in marker


def test_sticker_marker_keeps_format_information():
    assert describe(_sticker(animated=True)) == "〔動態貼圖〕"
    assert describe(_sticker(video=True)) == "〔影片貼圖〕"


def test_photo_marker():
    message = SimpleNamespace(sticker=None, photo=[SimpleNamespace(file_id="p")], document=None)
    assert describe(message) == "〔圖片〕"


def test_pick_static_sticker_uses_its_own_file():
    file_id, source = pick_file(_sticker())
    assert (file_id, source) == ("sticker-id", "sticker")


def test_pick_animated_sticker_falls_back_to_thumbnail():
    """動態貼圖無法當圖片開，改用 Telegram 附的第一格靜態縮圖。"""
    file_id, source = pick_file(_sticker(animated=True))
    assert (file_id, source) == ("thumb-id", "sticker")


def test_pick_video_sticker_falls_back_to_thumbnail():
    file_id, source = pick_file(_sticker(video=True))
    assert (file_id, source) == ("thumb-id", "sticker")


def test_animated_sticker_without_thumbnail_is_skipped():
    assert pick_file(_sticker(animated=True, thumbnail=None)) is None


def test_pick_photo_uses_the_largest_size():
    small = SimpleNamespace(file_id="small")
    large = SimpleNamespace(file_id="large")
    message = SimpleNamespace(sticker=None, photo=[small, large], document=None)
    assert pick_file(message) == ("large", "photo")


def test_pick_image_document():
    message = SimpleNamespace(
        sticker=None,
        photo=None,
        document=SimpleNamespace(file_id="doc", mime_type="image/png"),
    )
    assert pick_file(message) == ("doc", "photo")


def test_pick_non_image_document_is_ignored():
    message = SimpleNamespace(
        sticker=None,
        photo=None,
        document=SimpleNamespace(file_id="doc", mime_type="application/pdf"),
    )
    assert pick_file(message) is None


def test_pick_plain_text_returns_nothing():
    message = SimpleNamespace(sticker=None, photo=None, document=None)
    assert pick_file(message) is None
