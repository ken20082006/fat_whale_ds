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
    # 主要這一則自己貢獻了幾張。標註要寫對格數，所以得和引用的那一則分開算。
    own_count = 0

    targets = [message]
    if include_reply and message.reply_to_message is not None:
        targets.append(message.reply_to_message)

    for target in targets:
        picked = media.pick_file(target)
        if picked is None:
            continue

        # 下載前先看 Telegram 宣告的大小，不必先把整個檔案拉下來才知道它太大。
        # 不是每種媒體都附這個值，沒有的話交給 _download 在下載後再擋。
        if (
            picked.declared_bytes is not None
            and picked.declared_bytes > cfg.image_max_bytes
        ):
            logger.info(
                "略過過大的媒體：%s（%d bytes）", picked.source, picked.declared_bytes
            )
            if target is message:
                await message.reply_text(media.TOO_BIG)
                return None, []
            continue

        try:
            collected = await media.collect_media(bot, picked, cfg)
        except MediaError as exc:
            # 引用的那一則抓不到不該讓整則訊息失敗
            if target is message:
                await message.reply_text(str(exc))
                return None, []
            continue

        if target is message:
            own_count = len(collected)
        images.extend(collected)

    text = message.text or message.caption or ""

    # 標註只寫主要的這一則。被引用的那一則在引用串裡有自己的紀錄，
    # 而且它的媒體型態與這一則無關 —— 拿這一則去 describe 會寫出「〔檔案〕」。
    if own_count:
        hint = media.describe(message, frames=own_count)
        text = f"{text}\n{hint}".strip() if text else hint

    if not text and not images:
        return None, []  # 語音等尚未支援的型態

    return text, images
