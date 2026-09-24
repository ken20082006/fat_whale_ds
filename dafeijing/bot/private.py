"""私聊處理。"""

from __future__ import annotations

import logging
import re

from telegram import Update
from telegram.ext import ContextTypes

from ..core.chat import ChatRequest
from ..llm.openrouter import LLMError
from .commands import get_services
from .ingest import collect
from .ui import reply_markdown, reply_plain, send_sticker, typing

logger = logging.getLogger(__name__)

# 邀請碼輸入的寬鬆比對：允許 DFJ-XXXX-XXXX、dfj7k2m9qx4、DFJ 7K2M 9QX4 等寫法
_CODE_HINT = re.compile(r"^[Dd][Ff][Jj][\s\-_]*[A-Za-z0-9]{4}[\s\-_]*[A-Za-z0-9]{4}$")


async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """私聊的所有非指令訊息：文字、圖片、貼圖、檔案。"""
    svc = get_services(context)
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    # 未授權者：只有文字才可能是邀請碼
    if not svc.is_admin(user.id) and not await svc.access.is_active(user.id):
        if message.text and _CODE_HINT.match(message.text.strip()):
            result = await svc.access.redeem(
                user.id, message.text, user.full_name, user.username
            )
            if result.status.value == "ok":
                await message.reply_text("邀請碼確認。本鯨記住你了，說吧，要什麼。")
            else:
                await message.reply_text("這串碼沒用。確認一下，或找邀請你的人再要一張。")
        else:
            await message.reply_text("本鯨是私人養的。請輸入邀請碼，或點邀請連結進來。")
        return

    allowed, wait = svc.limiter.check(user.id)
    if not allowed:
        await message.reply_text(f"慢一點，{wait} 秒後再來。")
        return

    text, images, media_note = await collect(
        message, context.bot, svc.cfg, llm=svc.llm, db=svc.db
    )
    if text is None:
        await message.reply_text("這種訊息本鯨還讀不懂。用文字、圖片或貼圖都可以。")
        return

    merged = await svc.debouncer.gather(f"private:{user.id}", text, images)
    if merged is None:
        return  # 已併入前一輪
    text, images = merged

    await svc.access.ensure(
        user.id, user.full_name, user.username, is_admin=svc.is_admin(user.id)
    )
    session = await svc.sessions.private_session(user.id)

    request = ChatRequest(
        tg_user_id=user.id,
        display_name=user.full_name,
        text=text,
        chat_id=message.chat_id,
        session=session,
        images=images,
        media_note=media_note,
    )

    try:
        async with typing(context.bot, message.chat_id):
            outcome = await svc.chat.respond(request)
    except LLMError as exc:
        await reply_plain(message, str(exc))
        return
    except Exception:
        logger.exception("私聊回覆失敗")
        svc.errors += 1
        await reply_plain(message, "本鯨這邊出了點狀況，已經記在日誌裡了。")
        return

    await reply_markdown(message, outcome.text)

    if outcome.sticker_file_id:
        await send_sticker(
            context.bot, message.chat_id, outcome.sticker_file_id, reply_to=message.message_id
        )
