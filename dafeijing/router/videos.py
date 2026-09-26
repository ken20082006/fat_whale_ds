"""影片轉發 —— 落一個 Hermes 讀得到嘅檔，再叫佢用 `video_analyze` 睇。

**為什麼要落檔**：Hermes 嘅 API 只收 `input_image`，`input_file` 會回
`400 unsupported_content_type`。而 `video_analyze` 係一個**工具**，收
檔案路徑或者 URL，唔收 data URL。

Router 跑喺主機、Hermes 跑喺容器，所以同一個檔有兩個路徑：

    主機寫入  `<FW_HERMES_MEDIA_DIR>/abc.mp4`
    容器讀取  `<FW_HERMES_MEDIA_PREFIX>/abc.mp4`

`hermes_ds/data` 掛咗做 `/opt/data`，所以喺 data 底下寫就兩邊都見到。

**邊個負責睇**：模型自己叫 `video_analyze`（Hermes 側已經開好，而且
`auxiliary.vision.model` 指咗去 `bytedance-seed/seed-2.0-mini`）。
Router 只負責將檔放喺佢掂得到嘅地方，同話佢知個檔喺邊 ——
模型分工留返喺 Hermes，唔喺 Router 自己打 API。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from ..core import media

logger = logging.getLogger(__name__)

# Hermes 嘅 `_VIDEO_MIME_TYPES` 收呢幾種（`vision_tools.py:942-950`）。
# 送錯格式佢會回「Unsupported video format」。
_SUFFIX_BY_MIME = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}

# 舊檔幾耐清一次。Router 冇 Hermes 嗰種 cache 機制，檔案會累積。
_PRUNE_AFTER_SECONDS = 6 * 60 * 60


def should_send_video(picked: media.Picked | None) -> bool:
    """呢個媒體應唔應該當影片落檔交畀 Hermes。"""
    if picked is None or not media.is_motion(picked):
        return False
    # 影片貼紙：縮圖已經當圖片送咗，唔使再落一條片。
    if picked.source == "sticker_motion":
        return False
    # 真 GIF 走 router/gif.py —— Hermes 收唔到，而且要用另一個模型。
    if picked.source == "animation" and picked.clip_file_id is None:
        return False
    return picked.clip_file_id is not None


def _prune(directory: Path) -> None:
    """清走舊檔。冇做嘅話呢個目錄會無限大。"""
    cutoff = time.time() - _PRUNE_AFTER_SECONDS
    try:
        for item in directory.iterdir():
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink(missing_ok=True)
    except OSError as exc:  # 清理失敗唔應該影響今次轉發
        logger.debug("清舊影片失敗：%s", exc)


async def save_video(bot, message, svc) -> str | None:
    """如果有影片，落檔並回傳**容器內**嘅路徑；否則回 None。

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

    try:
        blob = await media._download(  # noqa: SLF001
            bot, picked.clip_file_id, max_bytes=cfg.video_delegate_max_bytes
        )
    except media.MediaError as exc:
        logger.info("影片下載唔到，當冇：%s", exc)
        return None

    suffix = _SUFFIX_BY_MIME.get(media.sniff_mime(blob))
    if suffix is None:
        logger.info("影片格式唔支援（%s）", media.sniff_mime(blob))
        return None

    directory = Path(cfg.hermes_media_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        _prune(directory)
        # 用 Telegram 嘅穩定識別碼做檔名 —— 同一條片再傳會覆蓋同一個檔，
        # 唔會愈積愈多。
        name = f"{picked.unique_id or picked.clip_file_id}{suffix}"
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