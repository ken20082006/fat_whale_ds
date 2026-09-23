"""把一則 Telegram 訊息整理成「文字 + 圖片」的統一形式。

私聊與群組共用，免得兩邊各寫一套下載與轉換邏輯。
"""

from __future__ import annotations

import logging

from telegram import Message

from ..core import media
from ..core.media import MediaError, PreparedImage

logger = logging.getLogger(__name__)


async def collect(
    message: Message,
    bot,
    cfg,
) -> tuple[str | None, list[PreparedImage]]:
    """回傳 (文字, 圖片列表)。不支援的訊息型態回傳 (None, [])。"""
    images: list[PreparedImage] = []

    picked = media.pick_file(message)
    if picked is not None:
        file_id, source = picked
        try:
            images.append(
                await media.prepare_from_telegram(
                    bot,
                    file_id,
                    max_edge=cfg.image_max_edge,
                    source=source,
                )
            )
        except MediaError as exc:
            await message.reply_text(str(exc))
            return None, []

    text = message.text or message.caption or ""

    if not text and not images:
        return None, []  # 語音、影片、檔案等尚未支援

    if images:
        hint = media.describe(message)
        text = f"{text}\n{hint}".strip() if text else hint

    return text, images
