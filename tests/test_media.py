"""媒體的標註、挑選與取格。

這一組是回歸規格。曾經的實況是：GIF 走 message.animation，而 pick_file
完全沒有這個分支，於是整則訊息被靜靜丟掉 —— 使用者只覺得 bot 忽然聾了。

影片與圓形影片訊息同理。三種都補上之後，這裡就是「哪種媒體走哪條路、
取幾格」的唯一書面依據。
"""

from __future__ import annotations

import asyncio
import io
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from dafeijing.core import media
from dafeijing.core.media import (
    Picked,
    collect_media,
    describe,
    pick_file,
)
from dafeijing.store.db import Database

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
    return SimpleNamespace(
        file_id=file_id, file_unique_id=f"{file_id}-uid", file_size=size
    )


def _sticker(*, animated=False, video=False, emoji="😭", thumbnail="thumb-id"):
    return SimpleNamespace(
        is_animated=animated,
        is_video=video,
        emoji=emoji,
        file_id="sticker-id",
        file_unique_id="sticker-uid",
        file_size=5000,
        # **刻意沒有 duration** —— 真實的 telegram.Sticker 沒有這個屬性。
        # 之前這裡為了迎合程式碼而加過，結果掩蓋了一個真 bug：影片貼圖
        # 會在 _clip() 裡 AttributeError，然後全局錯誤處理器就在群組回覆，
        # bot 冇被叫都自己出現。假物件比現實寬鬆，比缺屬性更危險。
        thumbnail=_thumb(thumbnail) if thumbnail else None,
    )


def _animation(*, mime="image/gif", thumbnail="thumb-id"):
    return SimpleNamespace(
        file_id="anim-id",
        file_unique_id="anim-uid",
        file_size=900_000,
        duration=6.0,
        mime_type=mime,
        thumbnail=_thumb(thumbnail) if thumbnail else None,
    )


def _video(*, thumbnail="thumb-id", size=3_000_000_000, duration=30.0):
    return SimpleNamespace(
        file_id="video-id",
        file_unique_id="video-uid",
        file_size=size,
        duration=duration,
        thumbnail=_thumb(thumbnail) if thumbnail else None,
    )


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (255, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


def _cfg(*, max_bytes=8 * 1024 * 1024):
    return SimpleNamespace(
        image_max_edge=1024,
        image_max_bytes=max_bytes,
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


def test_static_media_never_mentions_frames():
    message = _message(photo=[SimpleNamespace(file_id="p", file_size=1)])
    assert describe(message) == "〔圖片〕"



def test_collect_media_only_handles_static_media():
    """影片與動圖不走這條路 —— 一律外包，所以這裡只剩一張靜態圖。"""
    bot = _FakeBot(_png_bytes())
    images = asyncio.run(collect_media(bot, Picked("p", "photo", 100), _cfg()))
    assert len(images) == 1
    assert images[0].source == "photo"


def test_unknown_media_falls_back_to_a_generic_marker():
    assert describe(_message()) == "〔檔案〕"


# ── 挑選：貼圖 ──────────────────────────────────────────


def test_pick_static_sticker_uses_its_own_file():
    picked = pick_file(_message(sticker=_sticker()))
    assert (picked.file_id, picked.source) == ("sticker-id", "sticker")


def test_pick_animated_sticker_falls_back_to_thumbnail():
    """動態貼圖（.tgs）是 Lottie JSON，外包看唔到 —— 只有縮圖那張靜態圖。"""
    picked = pick_file(_message(sticker=_sticker(animated=True)))
    assert (picked.file_id, picked.source) == ("thumb-id", "sticker")


def test_pick_video_sticker_carries_its_body_for_outsourcing():
    """影片貼紙（.webm）是真的影片 —— 外包要拿本體，不是縮圖。

    而且要認得出它是 motion，否則會被當成靜態圖、只看到一格。
    """
    picked = pick_file(_message(sticker=_sticker(video=True)))
    assert (picked.file_id, picked.source) == ("thumb-id", "sticker_motion")
    assert picked.clip_file_id == "sticker-id"
    assert media.is_motion(picked)
    # 貼圖的 file_unique_id 永久穩定 —— 快取鍵靠它，所以睇一次就夠。
    assert picked.unique_id == "sticker-uid"


def test_animated_sticker_without_thumbnail_is_skipped():
    assert pick_file(_message(sticker=_sticker(animated=True, thumbnail=None))) is None


# ── 挑選：動圖 ──────────────────────────────────────────


def test_gif_downloads_the_file_itself():
    """真正的 GIF：file_id 就是本體，外包直接拿它去給會看片的模型。"""
    picked = pick_file(_message(animation=_animation(mime="image/gif")))
    assert (picked.file_id, picked.source) == ("anim-id", "animation")
    # 真 GIF 不需要另一條「本體」路 —— 它本身就是本體
    assert picked.clip_file_id is None


def test_mp4_animation_offers_its_body():
    """被 Telegram 轉成 MP4 的動圖：縮圖不解，本體才是外包要看的東西。"""
    picked = pick_file(_message(animation=_animation(mime="video/mp4")))
    assert (picked.file_id, picked.source) == ("thumb-id", "animation")
    assert picked.clip_file_id == "anim-id"
    assert picked.clip_seconds == 6.0


def test_animation_without_thumbnail_and_without_mp4_is_skipped():
    message = _message(animation=_animation(mime="video/mp4", thumbnail=None))
    assert pick_file(message) is None


# ── 挑選：影片 ──────────────────────────────────────────


def test_video_keeps_the_thumbnail_and_offers_the_body():
    """縮圖是退路，本體才是外包看片的來源。"""
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
    small = SimpleNamespace(file_id="small", file_unique_id="small-uid", file_size=100)
    large = SimpleNamespace(file_id="large", file_unique_id="large-uid", file_size=900)
    picked = pick_file(_message(photo=[small, large]))
    assert (picked.file_id, picked.source) == ("large", "photo")
    # unique_id 是外包描述快取的鍵 —— 少了它同一條片會每次重新外包、答案唔同
    assert picked.unique_id == "large-uid"


def test_pick_image_document():
    message = _message(
        document=SimpleNamespace(
            file_id="doc",
            file_unique_id="doc-uid",
            file_size=1234,
            mime_type="image/png",
        )
    )
    picked = pick_file(message)
    assert (picked.file_id, picked.source) == ("doc", "photo")
    assert picked.unique_id == "doc-uid"


def test_pick_non_image_document_is_ignored():
    message = _message(
        document=SimpleNamespace(
            file_id="doc", file_size=1234, mime_type="application/pdf"
        )
    )
    assert pick_file(message) is None


def test_pick_plain_text_returns_nothing():
    assert pick_file(_message()) is None



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


# ── 外包看片 ────────────────────────────────────────────
#
# 抽格只看得到幾個瞬間。有些模型直接吃得了整段影片，所以把片丟給它看完、
# 拿一段文字回來。下面守住「哪種媒體值得外包」與「mime 判斷」。


def test_is_motion_covers_the_three_dynamic_sources():
    """影片、圓形影片、動圖（GIF 或無聲 MP4）都值得外包。"""
    for source in ("video", "video_note", "animation"):
        assert media.is_motion(Picked("f", source)), source

    # 靜態的不必外包 —— 它本來就是一張圖
    for source in ("photo", "sticker"):
        assert not media.is_motion(Picked("f", source)), source


def test_is_motion_source_agrees_with_is_motion():
    """引用串只有來源字串，沒有 Picked —— 兩個判斷必須一致。

    不一致的話，引用串裡的影片會被抽格那條路收走，而抽一格看不出連續動作。
    """
    for source in ("video", "video_note", "animation", "sticker_motion"):
        assert media.is_motion_source(source), source
        assert media.is_motion(Picked("f", source)), source

    # 靜態貼圖與動態貼圖（.tgs）都不是影片
    for source in ("photo", "sticker"):
        assert not media.is_motion_source(source), source

    assert not media.is_motion_source(None)


def test_sniff_mime_tells_gif_from_mp4():
    """data URL 的 mime 不能猜錯，模型會照它解碼。

    Telegram 的「動圖」可能是 GIF 也可能是被轉過的 MP4，所以看來源不準，
    要看檔頭。
    """
    assert media.sniff_mime(b"GIF89a" + b"\x00" * 32) == "image/gif"
    assert media.sniff_mime(b"GIF87a" + b"\x00" * 32) == "image/gif"

    # EBML 檔頭 = webm / mkv
    assert media.sniff_mime(b"\x1a\x45\xdf\xa3" + b"\x00" * 32) == "video/webm"

    # 其餘（含一般 MP4 的 ftyp box）一律當 mp4
    assert media.sniff_mime(b"\x00\x00\x00\x20ftypisom") == "video/mp4"
    assert media.sniff_mime(b"") == "video/mp4"


class _DelegateCfg:
    video_delegate_max_bytes = 1024
    video_delegate_max_seconds = 60.0
    video_delegate_model = "test/model"
    video_delegate_max_tokens = 100
    video_delegate_max_chars = 300


# ── 外包描述的快取 ──────────────────────────────────────
#
# 同一條片再傳，外包模型會給出**唔同**的描述（實測同一條 GIF 三次得到
# 「鯨魚噴水」「掀檯」「街頭窄巷」）。貼圖更是同一張會反覆出現，而貼圖的
# file_unique_id 永久穩定 —— 睇一次就夠。所以快取不只是省錢，也是為了一致。


def test_description_is_cached_by_unique_id():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Database(Path(tmp) / "t.db")
            await db.connect()

            assert await media.get_cached_note(db, "u1") is None
            await media.remember_note(db, "u1", "animation", "一隻貓行過", "test/model")
            assert await media.get_cached_note(db, "u1") == "一隻貓行過"

            # 不同的媒體互不影響
            assert await media.get_cached_note(db, "u2") is None

            # 同一個 id 再寫是覆蓋，不是新增一列
            await media.remember_note(db, "u1", "animation", "換咗描述", "test/model")
            assert await media.get_cached_note(db, "u1") == "換咗描述"
            assert await db.fetchval("SELECT COUNT(*) FROM media_notes") == 1

            await db.close()

    asyncio.run(scenario())


def test_cache_is_skipped_without_an_id_or_a_db():
    """沒 db 或沒識別碼時要安靜地 no-op，不可以爆。"""

    async def scenario() -> None:
        assert await media.get_cached_note(None, "u1") is None
        assert await media.get_cached_note(object(), None) is None
        await media.remember_note(None, "u1", "video", "x", "m")
        await media.remember_note(object(), None, "video", "x", "m")

    asyncio.run(scenario())


def test_describe_video_is_skipped_without_an_llm():
    """沒有 llm 時直接跳過，呼叫端不該多付一次呼叫。"""
    picked = Picked("f", "video", clip_file_id="c", clip_seconds=5.0)
    assert asyncio.run(media.describe_video(None, picked, _DelegateCfg(), None)) is None



def test_describe_video_reports_why_it_skipped_instead_of_failing_silently():
    """太大或太長是**刻意的決定**（省錢），不是失敗 —— 所以要帶一句話出來。

    靜靜退回抽格的話，使用者會以為整段都被看過了，而實際上只看得到幾格。
    兩種原因要分開講，因為大小上限往往比長度上限更早觸發（一條 5 分鐘的
    720p 片通常遠超 10MB），使用者才知是哪一種。
    """
    # 宣告大小就超標 → 連下載都不必
    too_big = Picked("f", "video", clip_file_id="c", clip_bytes=4096, clip_seconds=5.0)
    note = asyncio.run(media.describe_video(None, too_big, _DelegateCfg(), object()))
    assert note is not None and not note.text
    assert "太大" in note.skipped

    # 太長 → 同理，但講的是長度
    too_long = Picked("f", "video", clip_file_id="c", clip_bytes=512, clip_seconds=600.0)
    note = asyncio.run(media.describe_video(None, too_long, _DelegateCfg(), object()))
    assert note is not None and not note.text
    assert "長過" in note.skipped
