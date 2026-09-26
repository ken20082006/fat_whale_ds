"""圖片轉發 —— 邊啲媒體送去 Hermes 嘅 vision。

**影片唔送**：Hermes 收片要一個佢讀得到嘅**路徑**，唔收 data URL
（`input_file` 會 400）。呢個係已知未做嘅一嚿。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from dafeijing.core.media import Picked
from dafeijing.router.hermes import _build_input
from dafeijing.router.images import collect_images, should_send_image


# ── 邊啲送 ──────────────────────────────────────────────


def test_photo_is_sent():
    assert should_send_image(Picked("f", "photo", 100))


def test_static_sticker_is_sent():
    assert should_send_image(Picked("f", "sticker", 100))


def test_video_sticker_sends_its_thumbnail():
    """影片貼紙（.webm）—— 送縮圖好過完全睇唔到。"""
    picked = Picked("thumb", "sticker_motion", 100, clip_file_id="body")
    assert should_send_image(picked)


def test_real_gif_is_not_sent_here():
    """真 GIF 走 router/gif.py —— 佢要用 gif_delegate_model 經 image_url 送，
    同呢條路（Hermes vision）唔同。"""
    assert not should_send_image(Picked("f", "animation", 100))


def test_video_is_not_sent():
    assert not should_send_image(Picked("f", "video", 100, clip_file_id="c"))


def test_video_note_is_not_sent():
    assert not should_send_image(Picked("f", "video_note", 100, clip_file_id="c"))


def test_mp4_animation_is_not_sent():
    assert not should_send_image(Picked("f", "animation", 100, clip_file_id="c"))


def test_nothing_is_not_sent():
    assert not should_send_image(None)


# ── API 格式 ────────────────────────────────────────────


def test_no_images_sends_a_bare_string():
    """冇圖就唔好包多層 —— 陣列格式係有需要先用。"""
    assert _build_input("你好", None) == "你好"
    assert _build_input("你好", []) == "你好"


def test_images_use_the_responses_format():
    """Hermes `/v1/responses` 嘅 inline image 格式（見 api-server.md）：
    `input` 做陣列，`content` 入面用 `input_text` / `input_image`。"""
    built = _build_input("睇下", ["data:image/jpeg;base64,AAA"])

    assert isinstance(built, list) and len(built) == 1
    assert built[0]["role"] == "user"
    parts = built[0]["content"]
    assert parts[0] == {"type": "input_text", "text": "睇下"}
    assert parts[1] == {"type": "input_image", "image_url": "data:image/jpeg;base64,AAA"}


def test_multiple_images_all_included():
    built = _build_input("兩張", ["data:image/jpeg;base64,A", "data:image/jpeg;base64,B"])
    assert [p["type"] for p in built[0]["content"]] == [
        "input_text",
        "input_image",
        "input_image",
    ]


# ── 抓圖失敗唔可以拖垮整則訊息 ──────────────────────────


class _Boom:
    def __getattr__(self, name):
        raise AssertionError(f"唔應該用到 {name}")


def _svc(cfg=None):
    return SimpleNamespace(cfg=cfg or _Boom())


def test_no_media_returns_empty_without_touching_anything():
    message = SimpleNamespace(
        sticker=None, animation=None, video=None, video_note=None, photo=None, document=None
    )
    assert asyncio.run(collect_images(None, message, _svc())) == []


def test_video_returns_empty_without_downloading():
    """影片唔喺呢條路 —— 連 cfg 都唔應該掂。"""
    message = SimpleNamespace(
        sticker=None,
        animation=None,
        video=SimpleNamespace(
            file_id="v", file_size=100, duration=5, mime_type="video/mp4",
            thumbnail=None, file_unique_id="u",
        ),
        video_note=None,
        photo=None,
        document=None,
    )
    assert asyncio.run(collect_images(None, message, _svc())) == []