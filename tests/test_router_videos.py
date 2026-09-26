"""影片轉發 —— 落一個 Hermes 讀得到嘅檔，再叫佢自己用 `video_analyze` 睇。

**為什麼要落檔**：Hermes 嘅 API 只收 `input_image`，`input_file` 會回
400。而 `video_analyze` 係工具，收路徑唔收 data URL。
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

from dafeijing.core.media import MediaError, Picked
from dafeijing.router.videos import save_video, should_send_video, video_note


# ── 邊啲落檔 ────────────────────────────────────────────


def test_video_is_sent():
    assert should_send_video(Picked("thumb", "video", 100, clip_file_id="body"))


def test_video_note_is_sent():
    assert should_send_video(Picked("thumb", "video_note", 100, clip_file_id="body"))


def test_mp4_animation_is_sent():
    """Telegram 將 GIF 轉成無聲 MP4 —— 嗰種 Hermes 睇得到。"""
    assert should_send_video(Picked("thumb", "animation", 100, clip_file_id="body"))


def test_real_gif_is_sent_here():
    """真 GIF 同影片行同一條路（2026-09-26 起）。

    之前真 GIF 由 Router 自己交外援模型（`xiaomi/mimo-v2.6-flash`）經
    `image_url` 睇，但實測佢**作出郁動** —— 一條「藍方塊固定、紅圓水平
    向右移」嘅測試 GIF，佢答「兩個都係垂直移動」。改為落檔之前轉做
    MP4，交返 Hermes 嗰條路（實測答得完全正確）。
    """
    assert should_send_video(Picked("f", "animation", 100))


def test_video_sticker_is_not_sent():
    """影片貼紙嘅縮圖已經當圖片送咗，唔使再落一條片。"""
    assert not should_send_video(Picked("thumb", "sticker_motion", 100, clip_file_id="body"))


def test_photo_is_not_sent():
    assert not should_send_video(Picked("f", "photo", 100))


def test_static_sticker_is_not_sent():
    assert not should_send_video(Picked("f", "sticker", 100))


def test_nothing_is_not_sent():
    assert not should_send_video(None)


def test_motion_source_without_a_body_is_not_sent():
    """理論上唔會發生，但冇本體就冇嘢可以落檔。"""
    assert not should_send_video(Picked("thumb", "video", 100))


# ── 給模型嘅提示 ────────────────────────────────────────


def test_video_note_gives_the_path():
    note = video_note("/opt/data/cache/videos/abc.mp4")
    assert "/opt/data/cache/videos/abc.mp4" in note


def test_video_note_tells_it_to_look():
    """唔講嘅話，模型會話「本鯨睇唔到」或者靠估。"""
    note = video_note("/x/y.mp4")
    assert "video_analyze" in note
    assert "睇唔到" in note


# ── 落檔 ────────────────────────────────────────────────


class _Svc:
    def __init__(self, directory: Path):
        self.cfg = SimpleNamespace(
            video_delegate_max_seconds=300.0,
            video_delegate_max_bytes=10 * 1024 * 1024,
            hermes_media_dir=directory,
            hermes_media_prefix="/opt/data/cache/videos",
        )


def _message(**attrs):
    base = dict(
        sticker=None, animation=None, video=None, video_note=None, photo=None, document=None
    )
    base.update(attrs)
    return SimpleNamespace(**base)


def _video_message(**over):
    fields = dict(
        file_id="body", file_size=1000, duration=5, mime_type="video/mp4",
        thumbnail=SimpleNamespace(file_id="thumb", file_size=100),
        file_unique_id="uid-1",
    )
    fields.update(over)
    return _message(video=SimpleNamespace(**fields))


def test_video_is_written_and_the_container_path_returned(monkeypatch):
    from dafeijing.core import media as media_mod
    from dafeijing.router import videos as videos_mod

    async def fake_download(_bot, _file_id, **_kw):
        return b"\x00\x00\x00\x18ftypmp42"

    monkeypatch.setattr(videos_mod.media, "_download", fake_download, raising=False)

    with tempfile.TemporaryDirectory() as tmp:
        path = asyncio.run(save_video(None, _video_message(), _Svc(Path(tmp))))

        assert path == "/opt/data/cache/videos/uid-1.mp4"
        assert (Path(tmp) / "uid-1.mp4").exists()


def test_same_video_twice_reuses_the_same_file(monkeypatch):
    """用 file_unique_id 做檔名 —— 同一條片再傳唔會愈積愈多。"""
    from dafeijing.router import videos as videos_mod

    async def fake_download(_bot, _file_id, **_kw):
        return b"\x00\x00\x00\x18ftypmp42"

    monkeypatch.setattr(videos_mod.media, "_download", fake_download, raising=False)

    with tempfile.TemporaryDirectory() as tmp:
        svc = _Svc(Path(tmp))
        first = asyncio.run(save_video(None, _video_message(), svc))
        second = asyncio.run(save_video(None, _video_message(), svc))

        assert first == second
        assert len(list(Path(tmp).iterdir())) == 1


def test_download_failure_is_swallowed(monkeypatch):
    """拎唔到片就當冇 —— 唔可以令整則訊息失敗。"""
    from dafeijing.router import videos as videos_mod

    async def boom(*_a, **_k):
        raise MediaError("拿不到")

    monkeypatch.setattr(videos_mod.media, "_download", boom, raising=False)

    with tempfile.TemporaryDirectory() as tmp:
        assert asyncio.run(save_video(None, _video_message(), _Svc(Path(tmp)))) is None


def test_overlong_video_is_skipped(monkeypatch):
    from dafeijing.router import videos as videos_mod

    async def fake_download(*_a, **_k):
        raise AssertionError("唔應該下載")

    monkeypatch.setattr(videos_mod.media, "_download", fake_download, raising=False)

    with tempfile.TemporaryDirectory() as tmp:
        message = _video_message(duration=9999)
        assert asyncio.run(save_video(None, message, _Svc(Path(tmp)))) is None


def test_gif_blob_is_transcoded_not_written_as_gif(monkeypatch):
    """落到嚟係 GIF 就走轉檔，唔會照寫個 .gif 出去。

    Hermes 嘅 `_VIDEO_MIME_TYPES` 冇 `.gif`（`vision_tools.py:942-950`），
    照送佢淨係會回 Unsupported video format。

    轉檔本身要真 ffmpeg，所以呢度只驗分流 —— 真 ffmpeg 由
    `scripts/` 之外嘅人手驗（見 commit 訊息）。
    """
    from dafeijing.router import videos as videos_mod

    async def fake_download(_bot, _file_id, **_kw):
        return b"GIF89a" + b"\x00" * 64

    async def fake_transcode(_blob, directory, stem):
        (directory / f"{stem}.mp4").write_bytes(b"fake-mp4")
        return directory / f"{stem}.mp4"

    monkeypatch.setattr(videos_mod.media, "_download", fake_download, raising=False)
    monkeypatch.setattr(videos_mod, "_gif_to_mp4", fake_transcode)

    with tempfile.TemporaryDirectory() as tmp:
        path = asyncio.run(save_video(None, _video_message(), _Svc(Path(tmp))))

        assert path == "/opt/data/cache/videos/uid-1.mp4"
        assert (Path(tmp) / "uid-1.mp4").exists()
        assert not (Path(tmp) / "uid-1.gif").exists()


def test_gif_transcode_failure_is_treated_as_absent(monkeypatch):
    """轉唔到就當冇呢個媒體。

    同大肥鯨一致：外包唔成唔會求其抽格充數。呢度亦係 ffmpeg 唔喺 PATH
    時嘅行為 —— Router image 冇裝 ffmpeg 就會行到呢一步（靜靜哋當冇）。
    """
    from dafeijing.router import videos as videos_mod

    async def fake_download(_bot, _file_id, **_kw):
        return b"GIF89a" + b"\x00" * 64

    async def fake_transcode(*_a, **_kw):
        return None

    monkeypatch.setattr(videos_mod.media, "_download", fake_download, raising=False)
    monkeypatch.setattr(videos_mod, "_gif_to_mp4", fake_transcode)

    with tempfile.TemporaryDirectory() as tmp:
        assert asyncio.run(save_video(None, _video_message(), _Svc(Path(tmp)))) is None


def test_unknown_format_falls_back_to_mp4(monkeypatch):
    """`sniff_mime` 認唔到嘅一律當 mp4 —— 大肥鯨原本嘅取捨。

    呢個係刻意的：與其因為認唔出而拒收，不如照送，等 Hermes 自己判斷。
    """
    from dafeijing.router import videos as videos_mod

    async def unknown(*_a, **_k):
        return b"\x00\x01\x02\x03"

    monkeypatch.setattr(videos_mod.media, "_download", unknown, raising=False)

    with tempfile.TemporaryDirectory() as tmp:
        path = asyncio.run(save_video(None, _video_message(), _Svc(Path(tmp))))
        assert path == "/opt/data/cache/videos/uid-1.mp4"


def test_old_files_are_pruned(monkeypatch):
    """冇清理嘅話呢個目錄會無限大。"""
    import os
    import time

    from dafeijing.router import videos as videos_mod

    async def fake_download(*_a, **_k):
        return b"\x00\x00\x00\x18ftypmp42"

    monkeypatch.setattr(videos_mod.media, "_download", fake_download, raising=False)

    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        stale = directory / "old.mp4"
        stale.write_bytes(b"x")
        old = time.time() - videos_mod._PRUNE_AFTER_SECONDS - 60
        os.utime(stale, (old, old))

        asyncio.run(save_video(None, _video_message(), _Svc(directory)))

        assert not stale.exists(), "舊檔應該清走"
        assert (directory / "uid-1.mp4").exists()