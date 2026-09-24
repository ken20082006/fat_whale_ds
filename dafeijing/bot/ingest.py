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
    db=None,
) -> tuple[str | None, list[PreparedImage]]:
    """回傳 (文字, 圖片列表)。不支援的訊息型態回傳 (None, [])。

    include_reply 控制要不要一併處理被引用的那一則的媒體。
    群組路徑會傳 False —— 那裡由引用串快取負責，已經涵蓋整條串，
    在這裡重複抓會多下載一次同一張圖。

    llm 有值時，動態素材會外包給吃得了影片的模型拿一段解說
    （見 media.describe_video）。**外包不成就當作沒有這個媒體** ——
    見下方「睇唔到就直接唔睇」。

    db 有值時，解說會按媒體的穩定識別碼快取 —— 同一條片再傳就重用同一個
    描述，既一致又免費。
    """
    images: list[PreparedImage] = []
    # 主要這一則自己貢獻了幾張。標註要寫對格數，所以得和引用的那一則分開算。
    own_count = 0
    own_note: media.VideoNote | None = None
    # 沒外包成功的原因（太長／太大／拿不到），要講給使用者聽。
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

        if media.is_motion(picked):
            # **影片與動圖一律外包，不在這裡抽格。** 主模型只看得懂靜態圖，
            # 而抽一格看不出連續動作，卻會讓它以為自己看過、講出半真半假的
            # 描述。外包成功就用解說，失敗就當作沒有這個媒體，並講出原因。
            note = await media.describe_video(bot, picked, cfg, llm, db=db)
            if note is not None and note.text:
                if target is message:
                    own_note = note
            elif note is not None and note.skipped and target is message:
                own_skip = note.skipped
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
    hint = ""
    if own_note is not None:
        # 明講這是「系統給你的內容描述」，不要只寫〔內容：…〕。
        # 實測後者會被當成中繼資料略過 —— 助理照樣答「我淨係知有張動圖」，
        # 而描述其實就在同一個 prompt 裡。
        hint = (
            f"{media.describe(message)}\n"
            f"〔系統給你的影片內容描述：{own_note.text}〕"
        )
    elif own_skip:
        # 刻意沒看就講出來 —— 否則對方會以為已經被看過了。
        hint = f"{media.describe(message)}\n〔{own_skip}〕"
    elif own_count:
        hint = media.describe(message)
    if hint:
        text = f"{text}\n{hint}".strip() if text else hint

    if not text and not images:
        return None, []  # 語音等尚未支援的型態

    return text, images
