"""輸出輔助：分段送出、打字指示、格式降級。

任何一則訊息都不該因為格式問題而送不出去 —— 轉換失敗就退回純文字。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from telegram import Message
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError

from ..render import split_html, to_telegram_html
from ..render.markdown import strip_markdown

logger = logging.getLogger(__name__)


async def reply_markdown(
    message: Message, markdown_text: str, *, reply: bool = True
) -> Message | None:
    """把模型輸出轉成 HTML 後分段送出。

    回傳第一段的 Message（群組需要它的 message_id 來補快取），失敗則回傳 None。
    """
    html = to_telegram_html(markdown_text)
    chunks = split_html(html) or [""]
    bot = message.get_bot()
    first: Message | None = None

    for index, chunk in enumerate(chunks):
        sent = await _send_chunk(
            bot,
            message.chat_id,
            chunk,
            reply_to=message.message_id if (index == 0 and reply) else None,
            fallback=markdown_text if index == 0 else chunk,
        )
        if sent is None:
            break  # 已經連續失敗，不再硬送
        if first is None:
            first = sent

    return first


async def send_markdown(bot, chat_id: int, markdown_text: str) -> None:
    html = to_telegram_html(markdown_text)
    for chunk in split_html(html) or [""]:
        await _send_chunk(bot, chat_id, chunk, reply_to=None, fallback=markdown_text)


async def _send_chunk(
    bot,
    chat_id: int,
    chunk: str,
    *,
    reply_to: int | None,
    fallback: str,
):
    """先試 HTML；格式被拒就退回純文字。網路中斷則直接放棄這一則。"""
    for parse_mode, payload in ((ParseMode.HTML, chunk), (None, strip_markdown(fallback))):
        try:
            return await _send_with_retry(bot, chat_id, payload, parse_mode, reply_to)
        except BadRequest as exc:
            logger.warning("送出失敗（%s），改用純文字重試", exc)
            continue
        except NetworkError as exc:
            # 網路問題換格式也沒用，別再白等一次逾時
            logger.warning("傳送時網路中斷（%s），放棄這一則", exc)
            return None

    logger.error("兩種格式都送不出去，chat=%s", chat_id)
    return None


async def _send_with_retry(bot, chat_id: int, payload: str, parse_mode, reply_to: int | None):
    """網路類錯誤重試一次；其餘直接往上拋交給呼叫端判斷。

    傳送失敗在真實環境很常見（連線逾時、Telegram 暫時 5xx），
    若不接住就會一路冒到全域錯誤處理，變成一則無意義的「未處理的例外」。
    """
    kwargs = {"parse_mode": parse_mode} if parse_mode else {}

    for attempt in range(2):
        try:
            return await bot.send_message(
                chat_id,
                payload,
                reply_to_message_id=reply_to,
                disable_web_page_preview=True,
                **kwargs,
            )
        except RetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
        except NetworkError:
            if attempt:
                raise
            await asyncio.sleep(1.0)

    return None


@contextlib.asynccontextmanager
async def typing(bot, chat_id: int) -> AsyncIterator[None]:
    """在處理期間持續顯示「正在輸入」。Telegram 的狀態五秒後會消失，所以循環補送。"""
    stop = asyncio.Event()

    async def loop() -> None:
        while not stop.is_set():
            with contextlib.suppress(TelegramError):
                await bot.send_chat_action(chat_id, ChatAction.TYPING)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=4.0)

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def reply_plain(message: Message, text: str) -> None:
    try:
        await message.reply_text(text, disable_web_page_preview=True)
    except TelegramError:
        logger.exception("純文字訊息送出失敗")
