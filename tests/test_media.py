"""媒體的標註、挑選與取格。

這一組是回歸規格。曾經的實況是：GIF 走 message.animation，而 pick_file
完全沒有這個分支，於是整則訊息被靜靜丟掉 —— 使用者只覺得 bot 忽然聾了。

影片與圓形影片訊息同理。三種都補上之後，這裡就是「哪種媒體走哪條路、
取幾格」的唯一書面依據。
"""

from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from dafeijing.core import media
from dafeijing.core.media import (
    Picked,
    collect_media,
    describe,
    frame_indexes,
    pick_file,
    prepare_gif_frames,
    sample_offsets,
)

# 真實的 telegram.Message 一定有這些屬性，所以 fake 也必須有 ——
# 漏掉的話 pick_file 會在存取時 AttributeError，而不是乾淨地回傳 None。
_FIELDS = ("sticker", "animation", "video", "video_note", "photo", "document")


def _message(**kwargs):
    fields: dict = dict.fromkeys(_FIELDS)
    unknown = set(kwargs) - set(_FIELDS)
    assert not unknown, f"未知欄位：{unknown}"
    fields.update(kwargs)
    return SimpleNamespace(**fields)


def _thumb(file_id="thumb-id", size=800):
    return SimpleNamespace(file_id=file_id, file_size=size)


def _sticker(*, animated=False, video=False, emoji="😭", thumbnail="thumb-id"):
    return SimpleNamespace(
        is_animated=animated,
        is_video=video,
        emoji=emoji,
        file_id="sticker-id",
        file_size=5000,
        thumbnail=_thumb(thumbnail) if thumbnail else None,
    )


def _animation(*, mime="image/gif", thumbnail="thumb-id"):
    return SimpleNamespace(
        file_id="anim-id",
        file_size=900_000,
        duration=6.0,
        mime_type=mime,
        thumbnail=_thumb(thumbnail) if thumbnail else None,
    )


def _video(*, thumbnail="thumb-id", size=3_000_000_000, duration=30.0):
    return SimpleNamespace(
        file_id="video-id",
        file_size=size,
        duration=duration,
        thumbnail=_thumb(thumbnail) if thumbnail else None,
    )


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (255, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


def _gif_bytes(frames: int = 6) -> bytes:
    """造一段真的動畫 GIF。每格顏色不同，方便驗證抽到的是不同格。"""
    images = [Image.new("RGB", (8, 8), (c, 0, 0)) for c in range(10, 10 + frames)]
    buffer = io.BytesIO()
    images[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=images[1:],
        duration=80,
        loop=0,
    )
    return buffer.getvalue()


def _cfg(*, frames=3, max_bytes=8 * 1024 * 1024, max_seconds=180.0):
    return SimpleNamespace(
        image_max_edge=1024,
        image_max_bytes=max_bytes,
        media_frames=frames,
        media_max_seconds=max_seconds,
    )


class _FakeBot:
    def __init__(self, blob: bytes) -> None:
        self._blob = blob
        self.calls: list[str] = []

    async def get_file(self, file_id):
        self.calls.append(file_id)
        blob = self._blob

        class _File:
            async def download_as_bytearray(inner):
                return bytearray(blob)

        return _File()


# ── 標註 ────────────────────────────────────────────────


def test_sticker_marker_omits_the_emoji():
    """emoji 是貼圖包作者標的，可能與畫面不符，不該餵給模型。"""
    marker = describe(_message(sticker=_sticker(emoji="😭")))
    assert marker == "〔貼圖〕"
    assert "😭" not in marker


def test_sticker_marker_keeps_format_information():
    assert describe(_message(sticker=_sticker(animated=True))) == "〔動態貼圖〕"
    assert describe(_message(sticker=_sticker(video=True))) == "〔影片貼圖〕"


def test_photo_marker():
    message = _message(photo=[SimpleNamespace(file_id="p", file_size=100)])
    assert describe(message) == "〔圖片〕"


def test_dynamic_marker_omits_the_frame_count_when_unknown():
    """引用串在快取階段只有文字，還不知道格數 —— 那時不該寫出可能錯的數字。"""
    assert describe(_message(video=_video())) == "〔影片〕"
    assert describe(_message(video_note=_video())) == "〔圓形影片〕"
    assert describe(_message(animation=_animation())) == "〔動圖〕"


def test_dynamic_marker_names_the_frame_count():
    """格數一定要寫出來，否則模型會以為自己看過整段。"""
    assert describe(_message(video=_video()), frames=3) == "〔影片，3 格畫面〕"
    assert describe(_message(video=_video()), frames=1) == "〔影片，只有第一格畫面〕"
    assert describe(_message(animation=_animation()), frames=3) == "〔動圖，3 格畫面〕"
    assert (
        describe(_message(sticker=_sticker(animated=True)), frames=1)
        == "〔動態貼圖，只有第一格畫面〕"
    )


def test_static_media_never_mentions_frames():
    message = _message(photo=[SimpleNamespace(file_id="p", file_size=1)])
    assert describe(message, frames=3) == "〔圖片〕"


def test_unknown_media_falls_back_to_a_generic_marker():
    assert describe(_message()) == "〔檔案〕"


# ── 挑選：貼圖 ──────────────────────────────────────────


def test_pick_static_sticker_uses_its_own_file():
    picked = pick_file(_message(sticker=_sticker()))
    assert (picked.file_id, picked.source) == ("sticker-id", "sticker")


def test_pick_animated_sticker_falls_back_to_thumbnail():
    """動態貼圖無法當圖片開，改用 Telegram 附的第一格靜態縮圖。"""
    picked = pick_file(_message(sticker=_sticker(animated=True)))
    assert (picked.file_id, picked.source) == ("thumb-id", "sticker")


def test_pick_video_sticker_falls_back_to_thumbnail():
    picked = pick_file(_message(sticker=_sticker(video=True)))
    assert (picked.file_id, picked.source) == ("thumb-id", "sticker")


def test_animated_sticker_without_thumbnail_is_skipped():
    assert pick_file(_message(sticker=_sticker(animated=True, thumbnail=None))) is None


# ── 挑選：動圖 ──────────────────────────────────────────


def test_gif_downloads_the_file_itself():
    """真正的 GIF：Pillow 解得開，下載本體逐格抽，不必屈就縮圖。"""
    picked = pick_file(_message(animation=_animation(mime="image/gif")))
    assert (picked.file_id, picked.source) == ("anim-id", "animation")
    # 真 GIF 不需要影片本體那條路
    assert picked.clip_file_id is None


def test_mp4_animation_offers_its_body_for_frame_extraction():
    """被 Telegram 轉成 MP4 的動圖：Pillow 開不了，交給 ffmpeg。"""
    picked = pick_file(_message(animation=_animation(mime="video/mp4")))
    assert (picked.file_id, picked.source) == ("thumb-id", "animation")
    assert picked.clip_file_id == "anim-id"
    assert picked.clip_seconds == 6.0


def test_animation_without_thumbnail_and_without_mp4_is_skipped():
    message = _message(animation=_animation(mime="video/mp4", thumbnail=None))
    assert pick_file(message) is None


# ── 挑選：影片 ──────────────────────────────────────────


def test_video_keeps_the_thumbnail_and_offers_the_body():
    """縮圖是退路，本體才是抽格的來源。"""
    picked = pick_file(_message(video=_video()))
    assert (picked.file_id, picked.source) == ("thumb-id", "video")
    assert picked.declared_bytes == 800  # 縮圖的大小，不是那 3 GB
    assert picked.clip_file_id == "video-id"
    assert picked.clip_bytes == 3_000_000_000
    assert picked.clip_seconds == 30.0


def test_video_note_is_handled_the_same_way():
    picked = pick_file(_message(video_note=_video()))
    assert (picked.file_id, picked.source) == ("thumb-id", "video_note")
    assert picked.clip_file_id == "video-id"


def test_video_without_thumbnail_is_skipped():
    assert pick_file(_message(video=_video(thumbnail=None))) is None
    assert pick_file(_message(video_note=_video(thumbnail=None))) is None


# ── 挑選：圖片與其他 ────────────────────────────────────


def test_pick_photo_uses_the_largest_size():
    small = SimpleNamespace(file_id="small", file_size=100)
    large = SimpleNamespace(file_id="large", file_size=900)
    assert pick_file(_message(photo=[small, large])) == Picked("large", "photo", 900)


def test_pick_image_document():
    message = _message(
        document=SimpleNamespace(file_id="doc", file_size=1234, mime_type="image/png")
    )
    assert pick_file(message) == Picked("doc", "photo", 1234)


def test_pick_non_image_document_is_ignored():
    message = _message(
        document=SimpleNamespace(
            file_id="doc", file_size=1234, mime_type="application/pdf"
        )
    )
    assert pick_file(message) is None


def test_pick_plain_text_returns_nothing():
    assert pick_file(_message()) is None


# ── 取樣位置 ────────────────────────────────────────────


def test_sample_offsets_avoids_head_and_tail():
    """開頭常是標題卡、結尾常是 logo，兩者都看不出內容。"""
    offsets = sample_offsets(100.0, 3)
    assert offsets == [15.0, 50.0, 85.0]
    assert all(0 < at < 100 for at in offsets)


def test_sample_offsets_are_evenly_spaced():
    offsets = sample_offsets(200.0, 3)
    gaps = [b - a for a, b in zip(offsets, offsets[1:])]
    assert len(set(round(g, 6) for g in gaps)) == 1


def test_sample_offsets_handles_unknown_or_zero_duration():
    assert sample_offsets(None, 3) == [0.0]
    assert sample_offsets(0.0, 3) == [0.0]


def test_sample_offsets_single_frame_takes_the_middle():
    assert sample_offsets(100.0, 1) == [50.0]


def test_frame_indexes_takes_everything_when_there_are_few_frames():
    assert frame_indexes(2, 3) == [0, 1]
    assert frame_indexes(3, 3) == [0, 1, 2]


def test_frame_indexes_spreads_across_the_middle():
    indexes = frame_indexes(100, 3)
    assert indexes == sorted(indexes)
    assert len(indexes) == 3
    assert all(0 < index < 100 for index in indexes)


def test_frame_indexes_never_repeats_and_stays_in_range():
    """格數很少時四捨五入可能撞在一起，不該回傳重複的格號。"""
    for total in range(2, 12):
        indexes = frame_indexes(total, 3)
        assert indexes == sorted(set(indexes))
        assert all(0 <= index < total for index in indexes)


# ── GIF 逐格抽取 ────────────────────────────────────────


def test_gif_frames_gives_distinct_frames():
    frames = prepare_gif_frames(_gif_bytes(6), max_edge=1024, source="animation", frames=3)
    assert len(frames) == 3
    # 每格顏色不同，所以內容必須真的不一樣
    assert len({f.data_url for f in frames}) == 3
    assert all(f.source == "animation" for f in frames)


def test_gif_frames_on_a_static_gif_gives_one():
    single = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(single, format="GIF")
    frames = prepare_gif_frames(
        single.getvalue(), max_edge=1024, source="animation", frames=3
    )
    assert len(frames) == 1


def test_gif_frames_caps_at_the_available_frame_count():
    frames = prepare_gif_frames(_gif_bytes(2), max_edge=1024, source="animation", frames=3)
    assert len(frames) == 2


# ── 取得畫面：決策路徑 ──────────────────────────────────
#
# 影片真正抽格要靠 ffmpeg，不放在單元測試裡 —— 這裡驗的是「該不該走那條路」。


def test_collect_media_single_frame_when_frames_disabled():
    bot = _FakeBot(_png_bytes())
    picked = Picked("thumb-id", "video", 800, "video-id", 900, 30.0)
    images = asyncio.run(collect_media(bot, picked, _cfg(frames=1)))
    assert len(images) == 1
    assert bot.calls == ["thumb-id"]


def test_collect_media_single_frame_when_the_clip_is_too_long():
    """太長的片只取一張 —— 完整解碼會讓回覆延遲到無法接受。"""
    bot = _FakeBot(_png_bytes())
    picked = Picked("thumb-id", "video", 800, "video-id", 900, 600.0)
    images = asyncio.run(collect_media(bot, picked, _cfg(max_seconds=180.0)))
    assert len(images) == 1
    assert bot.calls == ["thumb-id"]


def test_collect_media_single_frame_when_the_clip_is_too_big():
    bot = _FakeBot(_png_bytes())
    picked = Picked("thumb-id", "video", 800, "video-id", 50_000_000, 30.0)
    images = asyncio.run(collect_media(bot, picked, _cfg(max_bytes=8 * 1024 * 1024)))
    assert len(images) == 1
    assert bot.calls == ["thumb-id"]


def test_collect_media_single_frame_when_there_is_no_clip():
    """靜態貼圖與內嵌圖片沒有本體可抽格。"""
    bot = _FakeBot(_png_bytes())
    images = asyncio.run(collect_media(bot, Picked("p", "photo", 100), _cfg()))
    assert len(images) == 1
    assert bot.calls == ["p"]


def test_collect_media_samples_gif_frames_without_any_clip():
    """真 GIF 不必下載影片本體，Pillow 自己就能逐格抽。"""
    bot = _FakeBot(_gif_bytes(6))
    images = asyncio.run(collect_media(bot, Picked("g", "animation", 400), _cfg()))
    assert len(images) == 3
    assert bot.calls == ["g"]


def test_collect_media_does_not_sample_gif_frames_over_the_cap():
    """超過上限的 GIF 不該走多格那條路。

    多格與單張的分界在 collect_media，下載前的宣告大小守門則在呼叫端
    （ingest.collect）。這裡驗的是前者：它只會試單張一次，然後被實際大小擋下。
    """
    bot = _FakeBot(_gif_bytes(6))
    picked = Picked("g", "animation", 20_000_000)
    with pytest.raises(media.MediaError) as caught:
        asyncio.run(collect_media(bot, picked, _cfg(max_bytes=10)))
    assert str(caught.value) == media.TOO_BIG
    assert bot.calls == ["g"]  # 只試了一次，沒有先跑多格那條路


# ── 下載上限 ────────────────────────────────────────────


def test_download_over_the_cap_is_refused():
    """下載後再驗一次 —— Telegram 不是每種媒體都宣告大小。"""
    bot = _FakeBot(b"x" * 100)
    with pytest.raises(media.MediaError) as caught:
        asyncio.run(
            media.prepare_from_telegram(
                bot, "f", max_edge=1024, source="animation", max_bytes=10
            )
        )
    assert str(caught.value) == media.TOO_BIG


def test_download_within_the_cap_is_accepted():
    bot = _FakeBot(_png_bytes())
    prepared = asyncio.run(
        media.prepare_from_telegram(
            bot, "f", max_edge=1024, source="animation", max_bytes=1024
        )
    )
    assert prepared.source == "animation"
    assert prepared.data_url.startswith("data:image/jpeg;base64,")


def test_size_cap_is_not_enforced_when_unset():
    """沒有上限時不該用預設值偷偷擋人。"""
    prepared = asyncio.run(
        media.prepare_from_telegram(
            _FakeBot(_png_bytes()), "f", max_edge=1024, source="photo"
        )
    )
    assert prepared.width == 4
