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
    llm=None,
) -> tuple[str | None, list[PreparedImage]]:
    """回傳 (文字, 圖片列表)。不支援的訊息型態回傳 (None, [])。

    include_reply 控制要不要一併處理被引用的那一則的媒體。
    群組路徑會傳 False —— 那裡由引用串快取負責，已經涵蓋整條串，
    在這裡重複抓會多下載一次同一張圖。

    llm 有值時，動態素材會先外包給吃得了影片的模型拿一段解說
    （見 media.describe_video）。外包失敗就照舊抽格。
    """
    images: list[PreparedImage] = []
    # 主要這一則自己貢獻了幾張。標註要寫對格數，所以得和引用的那一則分開算。
    own_count = 0
    own_note: media.VideoNote | None = None
    # 刻意沒外包的原因（太長／太大）。有值就要講給使用者聽。
    own_skip = ""

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

        # 動態素材先試外包：它看得出連續動作、節奏與字幕變化，抽格做不到。
        note = None
        if media.is_motion(picked):
            note = await media.describe_video(bot, picked, cfg, llm)

        if note is not None and note.text:
            # 外包成功就**不再送圖** —— 解說已經涵蓋畫面內容，而且更省 token。
            if target is message:
                own_note = note
            continue

        # 沒外包成功就照舊抽格。「太長／太大」是刻意的決定（省錢），要記下來
        # 講給使用者聽；呼叫失敗則不必提 —— 那是我們自己的問題。
        if note is not None and note.skipped and target is message:
            own_skip = note.skipped

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
    hint = ""
    if own_note is not None:
        # frames=None：外包那條路沒有「幾格畫面」這回事，寫格數會是錯的。
        hint = f"{media.describe(message, frames=None)}\n〔內容：{own_note.text}〕"
    elif own_count:
        hint = media.describe(message, frames=own_count)
        if own_skip:
            # 刻意沒外包就講出來 —— 否則對方會以為整段都被看過了，
            # 而實際上只看得到幾格。
            hint = f"{hint}\n〔{own_skip}〕"
    if hint:
        text = f"{text}\n{hint}".strip() if text else hint

    if not text and not images:
        return None, []  # 語音等尚未支援的型態

    return text, images
