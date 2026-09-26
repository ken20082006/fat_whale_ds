"""影片轉發 —— 落一個 Hermes 讀得到嘅檔，再叫佢用 `video_analyze` 睇。

**真 GIF 都行呢條路**（2026-09-26 起）：落檔之前先轉做 MP4。
原本真 GIF 係 Router 自己交 `xiaomi/mimo-v2.6-flash` 經 `image_url` 睇，
但實測佢**作出郁動** —— 一條「藍方塊固定、紅圓水平向右移」嘅測試 GIF，
佢答「兩個都係垂直移動，方向相反」。同一個模型收到轉出嚟嘅 MP4
（經 `video_url`）之後答得**完全正確**，而且 15 秒變 3 秒。

所以 Router 而家**完全唔再打媒體 model** —— 只負責落檔同轉檔，
「睇」嘅嘢一律留喺 Hermes。

**為什麼要落檔**：Hermes 嘅 API 只收 `input_image`，`input_file` 會回
`400 unsupported_content_type`。而 `video_analyze` 係一個**工具**，收
檔案路徑或者 URL，唔收 data URL。

Router 跑喺主機、Hermes 跑喺容器，所以同一個檔有兩個路徑：

    主機寫入  `<FW_HERMES_MEDIA_DIR>/abc.mp4`
    容器讀取  `<FW_HERMES_MEDIA_PREFIX>/abc.mp4`

`hermes/data` 掛咗做 `/opt/data`，所以喺 data 底下寫就兩邊都見到。

**邊個負責睇**：模型自己叫 `video_analyze`（Hermes 側已經開好，而且
`auxiliary.vision.model` 指咗去 `bytedance-seed/seed-2.0-mini`）。
Router 只負責將檔放喺佢掂得到嘅地方，同話佢知個檔喺邊 ——
模型分工留返喺 Hermes，唔喺 Router 自己打 API。
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from ..core import media

logger = logging.getLogger(__name__)

# Hermes 嘅 `_VIDEO_MIME_TYPES` 收呢幾種（`vision_tools.py:942-950`）。
# 送錯格式佢會回「Unsupported video format」。
#
# ⚠️ `image/gif` **唔喺呢度** —— Hermes 收唔到真 GIF，要先轉做 MP4。見 `_gif_to_mp4`。
_SUFFIX_BY_MIME = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}

# 舊檔幾耐清一次。Router 冇 Hermes 嗰種 cache 機制，檔案會累積。
_PRUNE_AFTER_SECONDS = 6 * 60 * 60

# 轉 GIF 用。逾時當失敗 —— 一條 Telegram 動圖頂多幾 MB，正常一秒內搞掂。
_FFMPEG_TIMEOUT_SECONDS = 60.0


def should_send_video(picked: media.Picked | None) -> bool:
    """呢個媒體應唔應該當影片落檔交畀 Hermes。

    **真 GIF（`animation` 但冇 clip）包括在內** —— 佢會先轉做 MP4，
    見 `save_video`。呢個係 2026-09-26 嘅改動：之前真 GIF 由 Router
    自己交外援模型睇，但實測嗰個模型會作出郁動。
    """
    if picked is None or not media.is_motion(picked):
        return False
    # 影片貼紙：縮圖已經當圖片送咗，唔使再落一條片。
    if picked.source == "sticker_motion":
        return False
    # 有 clip ＝ Telegram 轉過嘅 MP4（或者真影片）；冇 clip 嘅 animation ＝ 真 GIF。
    return picked.clip_file_id is not None or picked.source == "animation"


def _prune(directory: Path) -> None:
    """清走舊檔。冇做嘅話呢個目錄會無限大。"""
    cutoff = time.time() - _PRUNE_AFTER_SECONDS
    try:
        for item in directory.iterdir():
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink(missing_ok=True)
    except OSError as exc:  # 清理失敗唔應該影響今次轉發
        logger.debug("清舊影片失敗：%s", exc)


async def _gif_to_mp4(blob: bytes, directory: Path, stem: str) -> Path | None:
    """真 GIF → MP4，寫入 `directory`，回傳最終檔。失敗回 None。

    **一定要落真檔，唔可以用 pipe。** ffmpeg 寫 MP4 要 seek 返轉頭補
    `moov` atom；經 stdout 就要加 `frag_keyframe`，出嚟係 fragmented MP4
    —— Hermes 食唔食未實測。落檔多一個 I/O，但結果同 Telegram 自己轉
    出嚟嗰啲一模一樣（實測 `ftypisom`）。

    失敗就當冇呢個媒體 —— 同大肥鯨一致：外包唔成唔會求其抽格充數。
    """
    src = directory / f"{stem}.gif"
    dst = directory / f"{stem}.mp4"
    ok = False
    try:
        src.write_bytes(blob)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(src),
            "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            # H.264 要雙數邊長，單數會 encoder 失敗。GIF 幾多奇數邊都有。
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            str(dst),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(
                proc.communicate(), timeout=_FFMPEG_TIMEOUT_SECONDS
            )
        except TimeoutError:
            # wait_for 取消咗 communicate，process 未必收得切 —— 補一刀。
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            logger.warning("GIF 轉 MP4 逾時（%.0f 秒）", _FFMPEG_TIMEOUT_SECONDS)
            return None
        if proc.returncode != 0:
            logger.warning(
                "GIF 轉 MP4 失敗（exit %s）：%s",
                proc.returncode,
                err.decode("utf-8", "replace")[:200],
            )
            return None
        ok = True
        return dst
    except OSError as exc:
        # ffmpeg 唔喺 PATH（FileNotFoundError）、冇權限、寫唔入 —— 全部 ⊂ OSError。
        #
        # ⚠️ 唔好寫 `asyncio.SubprocessError`：實測本機 venv（3.14）冇呢個名，
        # 一 raise 就變成 AttributeError，反而蓋過真正嘅錯。3.12 有，但唔值得靠。
        logger.warning("GIF 轉 MP4 出錯：%s", exc)
        return None
    finally:
        src.unlink(missing_ok=True)
        # 失敗要清走半截檔，唔係下次同一個 unique_id 會攞到爛片。
        if not ok:
            dst.unlink(missing_ok=True)


async def save_video(bot, message, svc) -> str | None:
    """如果有影片或者真 GIF，落檔並回傳**容器內**嘅路徑；否則回 None。

    過長或者過大就當冇 —— 同大肥鯨一致：外包唔成就當作冇呢個媒體，
    唔會求其抽格充數。
    """
    picked = media.pick_file(message)
    if not should_send_video(picked):
        return None

    cfg = svc.cfg
    if picked.clip_seconds is not None and picked.clip_seconds > cfg.video_delegate_max_seconds:
        logger.info("影片太長（%s 秒），唔轉發", picked.clip_seconds)
        return None

    # 真 GIF 冇 clip_*，`file_id` 本身就係 GIF 本體。
    file_id = picked.clip_file_id or picked.file_id
    if not file_id:
        return None

    try:
        blob = await media._download(  # noqa: SLF001
            bot, file_id, max_bytes=cfg.video_delegate_max_bytes
        )
    except media.MediaError as exc:
        logger.info("影片下載唔到，當冇：%s", exc)
        return None

    mime = media.sniff_mime(blob)
    directory = Path(cfg.hermes_media_dir)
    # 用 Telegram 嘅穩定識別碼做檔名 —— 同一條片再傳會覆蓋同一個檔，
    # 唔會愈積愈多。
    stem = picked.unique_id or picked.clip_file_id or "clip"

    try:
        directory.mkdir(parents=True, exist_ok=True)
        _prune(directory)

        if mime == "image/gif":
            written = await _gif_to_mp4(blob, directory, stem)
            if written is None:
                return None
            name = written.name
        else:
            suffix = _SUFFIX_BY_MIME.get(mime)
            if suffix is None:
                logger.info("影片格式唔支援（%s）", mime)
                return None
            name = f"{stem}{suffix}"
            (directory / name).write_bytes(blob)
    except OSError as exc:
        logger.warning("影片落檔失敗：%s", exc)
        return None

    return f"{cfg.hermes_media_prefix.rstrip('/')}/{name}"


def video_note(path: str) -> str:
    """話畀模型知個檔喺邊、叫佢自己去睇。

    措辭要似「陳述事實」而唔似「指令」—— 同大肥鯨嘅媒體標註一致。
    落喺訊息後面，同使用者講嘅嘢分開。
    """
    return (
        f"[這一則有一段影片，檔案喺 {path}。"
        "用 video_analyze 睇下內容再回應，唔好淨係話睇唔到。]"
    )
