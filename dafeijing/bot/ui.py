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
from telegram.error import BadRequest, RetryAfter, TelegramError

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
    for parse_mode, payload in ((ParseMode.HTML, chunk), (None, strip_markdown(fallback))):
        try:
            kwargs = {"parse_mode": parse_mode} if parse_mode else {}
            return await bot.send_message(
                chat_id,
                payload,
                reply_to_message_id=reply_to,
                disable_web_page_preview=True,
                **kwargs,
            )
        except RetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
        except BadRequest as exc:
            logger.warning("HTML 送出失敗（%s），改用純文字", exc)

    logger.error("訊息送出失敗，放棄。chat=%s", chat_id)
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
