"""Router 嘅 Telegram handlers。

流程：收到訊息 → 追串根 → 砌對話名 → 交去 Hermes → 回覆 → **補快取自己嘅回覆**。

同大肥鯨最大嘅分別：呢度冇 `svc.chat.respond()`，換成 `svc.hermes.ask()`。
其餘（授權、節流、引用串快取、渲染、送出）全部照用大肥鯨嗰套。
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from ..bot.commands import get_services
from ..bot.group import group_usable, is_addressed_to_bot
from ..bot.ui import reply_markdown, reply_plain, typing
from .conversation import (
    attribute,
    build_input,
    dm_conversation,
    group_conversation,
    pending_since_bot,
)
from .hermes import HermesError

logger = logging.getLogger(__name__)

_GROUP_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP)


def _is_group(chat) -> bool:
    return chat is not None and chat.type in _GROUP_TYPES


async def _deliver(message, bot, svc, conversation: str, text: str) -> None:
    """交去 Hermes 跟住回覆，最後補快取自己嗰則。

    **補快取呢步唔可以省。** Telegram 唔會將 bot 自己發嘅訊息回傳畀 bot，
    所以唔手動補嘅話，別人「回覆本鯨」時追唔到串根 ——
    嗰句會被當成一條**新串**，對話即刻斷開。
    """
    try:
        async with typing(bot, message.chat_id):
            reply = await svc.hermes.ask(conversation, text)
    except HermesError as exc:
        logger.warning("Hermes 失敗（%s）：%s", conversation, exc)
        svc.errors += 1
        await reply_plain(message, "本鯨這邊出了點狀況，等一下再試。")
        return

    sent = await reply_markdown(message, reply.text)
    if sent is not None:
        await svc.chain.cache_message(
            chat_id=message.chat_id,
            message_id=sent.message_id,
            reply_to_id=message.message_id,
            user_id=svc.bot_id,
            display_name=svc.bot_name,
            text=reply.text,
        )


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """群組：被 @ 或回覆本鯨 → 交去該條引用串嘅 Hermes 對話。"""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or not _is_group(message.chat):
        return

    svc = get_services(context)
    if not await group_usable(svc, context.bot, message.chat_id):
        return
    if not is_addressed_to_bot(message, svc):
        return

    allowed, wait = svc.limiter.check(user.id)
    if not allowed:
        await reply_plain(message, f"慢一點，{wait} 秒後再來。")
        return

    await svc.access.ensure(
        user.id, user.full_name, user.username, is_admin=svc.is_admin(user.id)
    )

    # Bot API 收唔到其他 bot 嘅訊息，所以被引用嗰則若唔喺快取就用即時更新補上，
    # 否則回溯到佢就斷。
    if message.reply_to_message is not None:
        await svc.chain.cache_from_update(message.reply_to_message)

    # 追串根 → 對話名。冇引用任何訊息嘅話串根就係呢則自己，即係開新對話。
    root = await svc.chain.root_id(message.chat_id, message.message_id)
    conversation = group_conversation(message.chat_id, root)

    # 只送「bot 上次發言之後」嘅新訊息 —— Hermes 已經有之前嘅歷史。
    # 但中間冇 @ 嘅人講嘅嘢都要送，否則 Hermes 完全唔知佢講過乜。
    chain = await svc.chain.resolve(message.chat_id, message.message_id)
    body = build_input(
        pending_since_bot(chain, svc.bot_id),
        (user.full_name, user.id, message.text or message.caption or ""),
    )

    await _deliver(message, context.bot, svc, conversation, body)


async def on_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """私聊：一條連續對話（有 topic 就跟 topic 分）。"""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or message.chat is None:
        return
    if message.chat.type != ChatType.PRIVATE:
        return

    svc = get_services(context)

    allowed, wait = svc.limiter.check(user.id)
    if not allowed:
        await reply_plain(message, f"慢一點，{wait} 秒後再來。")
        return

    await svc.access.ensure(
        user.id, user.full_name, user.username, is_admin=svc.is_admin(user.id)
    )

    thread_id = getattr(message, "message_thread_id", None)
    conversation = dm_conversation(message.chat_id, thread_id)
    body = attribute(user.full_name, user.id, message.text or message.caption or "")

    await _deliver(message, context.bot, svc, conversation, body)
