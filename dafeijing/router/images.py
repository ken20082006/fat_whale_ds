"""圖片轉發 —— Telegram 嘅靜態媒體送去 Hermes 嘅 vision。

大肥鯨原本自己做（`core/media.py` 嘅 `collect_media`），Router 照用同一段
code，只係終點由 OpenRouter 改成 Hermes API。相片本身仍然係本機處理 ——
下載、縮圖、鋪白底、轉 JPEG —— 冇多花一蚊。

**送邊啲**：

| 來源 | 送唔送 | 為咩 |
|---|---|---|
| `photo` | ✅ | 一般相片 |
| `sticker` | ✅ | 靜態貼圖；`.tgs` 動態貼圖送縮圖 |
| `sticker_motion` | ✅ | 影片貼圖（`.webm`）—— 送縮圖，好過完全睇唔到 |
| `animation` | ❌ | 真 GIF 走 `router/gif.py`；MP4 動圖未接 |
| `video` / `video_note` | ❌ | 未接（見 ARCHITECTURE.md 未解決清單） |

影片唔送係因為 Hermes 收片要一個佢讀得到嘅**路徑**，唔收 data URL
（`input_file` 會 400）。要另做一步落檔案，所以留返做下一嚿。
"""

from __future__ import annotations

import logging

from ..core import media

logger = logging.getLogger(__name__)

# 呢幾種來源有「一張靜態畫面」可以送。
_IMAGE_SOURCES = ("photo", "sticker", "sticker_motion")


def should_send_image(picked: media.Picked | None) -> bool:
    """呢個媒體應唔應該當圖片送去 Hermes。"""
    return picked is not None and picked.source in _IMAGE_SOURCES


async def collect_images(bot, message, svc) -> list[str]:
    """由一則訊息抽出可以送去 Hermes 嘅圖片（`data:image/jpeg;base64,...`）。

    抓唔到就回空 list —— 圖片係加分項，唔可以令整則訊息讀唔到。
    （大肥鯨 `media.py` 嘅同一個取捨。）
    """
    picked = media.pick_file(message)
    if not should_send_image(picked):
        return []

    try:
        prepared = await media.collect_media(bot, picked, svc.cfg)
    except media.MediaError as exc:
        logger.info("圖片抓唔到，當冇：%s", exc)
        return []

    return [image.data_url for image in prepared]