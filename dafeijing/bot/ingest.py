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
    *,
    include_reply: bool = True,
) -> tuple[str | None, list[PreparedImage]]:
    """回傳 (文字, 圖片列表)。不支援的訊息型態回傳 (None, [])。

    include_reply 控制要不要一併處理被引用的那一則的媒體。
    群組路徑會傳 False —— 那裡由引用串快取負責，已經涵蓋整條串，
    在這裡重複抓會多下載一次同一張圖。
    """
    images: list[PreparedImage] = []

    targets = [message]
    if include_reply and message.reply_to_message is not None:
        targets.append(message.reply_to_message)

    for target in targets:
        picked = media.pick_file(target)
        if picked is None:
            continue
        file_id, source = picked
        try:
            images.append(
                await media.prepare_from_telegram(
                    bot, file_id, max_edge=cfg.image_max_edge, source=source
                )
            )
        except MediaError as exc:
            # 引用的那一則抓不到不該讓整則訊息失敗
            if target is message:
                await message.reply_text(str(exc))
                return None, []

    text = message.text or message.caption or ""

    if not text and not images:
        return None, []  # 語音、影片、檔案等尚未支援

    if images:
        hint = media.describe(message)
        text = f"{text}\n{hint}".strip() if text else hint

    return text, images
