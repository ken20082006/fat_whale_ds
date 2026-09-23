"""群組處理。

兩件事完全分開：
1. 快取 —— 所有群組訊息都寫入短期快取，只為了還原引用串，永不送進模型。
2. 回應 —— 只有「被 @」或「回覆本鯨的訊息」才會觸發，且上下文限於該條引用串。
"""

from __future__ import annotations

import logging

from telegram import MessageEntity, Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from ..core.chat import ChatRequest
from ..llm.openrouter import LLMError
from .commands import get_services
from .ui import reply_markdown, reply_plain, typing

logger = logging.getLogger(__name__)


# ── 進出群組 ────────────────────────────────────────────


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """本鯨被加入或移出群組。

    Telegram 沒有「只准某人把我加進群組」的設定（BotFather 的 /setjoingroups
    是全有全無，連管理員也會一併擋掉）。所以改成在這裡判斷「是誰加的」：
    my_chat_member 更新帶有 from 欄位，非管理員加的一律立刻退出。
    """
    member = update.my_chat_member
    if member is None:
        return
    chat = member.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    status = member.new_chat_member.status
    if status in ("left", "kicked"):
        logger.info("已離開群組 %s", chat.id)
        return

    svc = get_services(context)
    added_by = member.from_user.id if member.from_user else None
    added_by_name = member.from_user.full_name if member.from_user else "未知"

    await svc.access.register_group(chat.id, chat.title, added_by)

    if svc.is_admin(added_by):
        await svc.access.set_group_allowed(chat.id, True)
        await context.bot.send_message(
            chat.id, "本鯨進來了。要找本鯨就 @ 我，或回覆本鯨的訊息。"
        )
        logger.info("管理員把本鯨加入群組「%s」（%s），已自動授權", chat.title, chat.id)
        return

    logger.warning(
        "非管理員 %s（%s）把本鯨加入群組「%s」（%s），立刻退出",
        added_by_name,
        added_by,
        chat.title,
        chat.id,
    )
    await context.bot.send_message(chat.id, "本鯨只認主人的邀請，先告退了。")
    await context.bot.leave_chat(chat.id)

    await _notify_admins(
        context,
        svc,
        f"有人把本鯨加入了群組，本鯨已退出。\n\n"
        f"　加入者：{added_by_name}（id: {added_by}）\n"
        f"　群組：{chat.title}（id: {chat.id}）\n\n"
        f"若這個群組沒問題，先執行 /allowgroup {chat.id}，再請對方重新把本鯨加進去。",
    )


async def _notify_admins(context: ContextTypes.DEFAULT_TYPE, svc, text: str) -> None:
    for admin_id in svc.cfg.admin_ids:
        try:
            await context.bot.send_message(admin_id, text)
        except Exception:
            logger.debug("通知管理員 %s 失敗", admin_id)


# ── 快取 ────────────────────────────────────────────────


async def cache_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """每個群組訊息都入快取。這是以隱私換取引用鏈回溯的能力。"""
    message = update.effective_message
    if message is None or message.chat is None:
        return

    svc = get_services(context)
    if not await svc.access.is_group_allowed(message.chat_id):
        return

    await svc.chain.cache_from_update(message)


# ── 回應 ────────────────────────────────────────────────


async def handle_group_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    svc = get_services(context)

    if message is None or user is None:
        return
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if not await svc.access.is_group_allowed(message.chat_id):
        return
    if not _is_addressed_to_bot(message, svc):
        return

    text = message.text or message.caption or ""
    if not text.strip() and not message.reply_to_message:
        return

    allowed, wait = svc.limiter.check(user.id)
    if not allowed:
        await message.reply_text(f"慢一點，{wait} 秒後再來。")
        return

    # 引用串回溯：從被指名的那則往上追到源頭
    chain = await svc.chain.resolve(message.chat_id, message.message_id)
    root_id = chain[0]["message_id"] if chain else message.message_id
    chain_text = svc.chain.format_for_prompt(chain, svc.bot_name)

    # 群組不做 debounce：每條串都是獨立事件，合併反而會混淆發言者
    session = await svc.sessions.group_thread_session(message.chat_id, root_id)

    request = ChatRequest(
        tg_user_id=user.id,
        display_name=user.full_name,
        text=text,
        chat_id=message.chat_id,
        session=session,
        is_group=True,
        chain_text=chain_text or None,
        chain_messages=chain,
    )

    try:
        async with typing(context.bot, message.chat_id):
            result = await svc.chat.respond(request)
    except LLMError as exc:
        await reply_plain(message, str(exc))
        return
    except Exception:
        logger.exception("群組回覆失敗")
        svc.errors += 1
        await reply_plain(message, "本鯨這邊出了點狀況。")
        return

    sent = await reply_markdown(message, result.text)
    if sent is not None:
        # 本鯨自己的訊息不會從 Telegram 收到，必須自己補進快取，
        # 否則別人回覆本鯨時，引用鏈會在這裡斷掉。
        await svc.chain.cache_message(
            chat_id=message.chat_id,
            message_id=sent.message_id,
            reply_to_id=message.message_id,
            user_id=svc.bot_id,
            display_name=svc.bot_name,
            text=result.text,
        )


def _is_addressed_to_bot(message, svc) -> bool:
    """被 @，或是回覆本鯨的訊息。"""
    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.id == svc.bot_id:
            return True

    if message.via_bot and message.via_bot.id == svc.bot_id:
        return True

    if not message.entities or not svc.bot_username:
        return False

    target = svc.bot_username.lower()
    for entity in message.entities:
        if entity.type != MessageEntity.MENTION:
            continue
        mentioned = (message.text or "")[entity.offset : entity.offset + entity.length]
        if mentioned.lstrip("@").lower() == target:
            return True
    return False
