"""真 GIF 例外 —— Router 自己睇，其餘媒體交畀 Hermes。

**為什麼要例外**：Hermes 嘅 `video_analyze` 收唔到真 GIF ——
`_VIDEO_MIME_TYPES` 冇 `.gif`，而且佢一律用 `video_url` 送。
大肥鯨實測（十個模型）只有 `xiaomi/mimo-v2.6-flash` 經 `image_url`
睇得到真 GIF，而其他模型**唔會報錯，會憑空作場景** —— 垃圾會寫入
`media_notes` 永久保存。
"""

from __future__ import annotations

from types import SimpleNamespace

from dafeijing.core.media import Picked
from dafeijing.router.gif import is_real_gif


def test_real_gif_is_recognised():
    """真 GIF：source 係 animation，而且**冇** clip（本體就係圖片）。"""
    assert is_real_gif(Picked("f", "animation", 1000, unique_id="u"))


def test_mp4_animation_is_not_a_real_gif():
    """Telegram 會將使用者上傳嘅 GIF 轉成無聲 MP4 —— 嗰種有 clip，
    要交返 Hermes（佢嘅 video_analyze 應付得來）。"""
    picked = Picked("f", "animation", 1000, clip_file_id="c", clip_bytes=5000)
    assert not is_real_gif(picked)


def test_video_is_not_a_gif():
    assert not is_real_gif(Picked("f", "video", 1000, clip_file_id="c"))


def test_static_image_is_not_a_gif():
    assert not is_real_gif(Picked("f", "photo", 1000))


def test_none_is_not_a_gif():
    assert not is_real_gif(None)


def test_sticker_motion_is_not_a_gif():
    """影片貼紙（.webm）係真影片，唔係 GIF。"""
    assert not is_real_gif(Picked("f", "sticker_motion", 1000, clip_file_id="c"))


# ── 只有真 GIF 先會觸發外包 ────────────────────────────


class _Boom:
    """碰到就爆 —— 證明非 GIF 完全冇行過外包條路。"""

    def __getattr__(self, name):
        raise AssertionError(f"唔應該用到 {name}")


def _svc():
    return SimpleNamespace(cfg=_Boom(), llm=_Boom(), db=_Boom())


def _message(**attrs):
    base = dict(
        sticker=None,
        animation=None,
        video=None,
        video_note=None,
        photo=None,
        document=None,
    )
    base.update(attrs)
    return SimpleNamespace(**base)


def test_no_media_returns_none_without_touching_the_model():
    import asyncio

    from dafeijing.router.gif import gif_note

    result = asyncio.run(gif_note(None, _message(), _svc()))
    assert result is None


def test_static_photo_returns_none_without_touching_the_model():
    """靜態圖交畀 Hermes 嘅 vision，Router 唔應該掂。"""
    import asyncio

    from dafeijing.router.gif import gif_note

    message = _message(photo=[SimpleNamespace(file_id="p", file_size=100, file_unique_id="u")])
    assert asyncio.run(gif_note(None, message, _svc())) is None


def test_video_returns_none_without_touching_the_model():
    import asyncio

    from dafeijing.router.gif import gif_note

    message = _message(
        video=SimpleNamespace(
            file_id="v", file_size=100, duration=5, mime_type="video/mp4",
            thumbnail=None, file_unique_id="u",
        )
    )
    assert asyncio.run(gif_note(None, message, _svc())) is None