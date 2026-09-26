"""Router 嘅 Telegram handlers。

流程：收到訊息 → 決定對話 → 交去 Hermes → 回覆 → **補快取自己嘅回覆**。

**「同一串」嘅定義（用戶定死）：只有引用本鯨嘅回答先算同一串。**

    用戶1 @bot              → 開新對話 A
    bot 回答 R1（記住 R1 屬於 A）
    用戶2 引用 R1           → 同一條對話 A
    用戶3 引用 用戶2         → **唔算** —— 開新對話

所以唔使追成條引用鏈：只需要記住本鯨每則回覆屬於邊條對話
（`group_cache.conversation`），引用本鯨嗰則時查返出嚟。

咁亦代表**每次只需要送觸發嗰一句**：一條對話入面除咗觸發訊息就係本鯨
自己嘅回覆，Hermes 已經有齊歷史，唔使重送。
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from ..bot.commands import get_services
from ..bot.group import group_usable, is_addressed_to_bot
from ..bot.ui import reply_markdown, reply_plain, typing
from ..core.session import scope_for
from .conversation import attribute, dm_conversation, group_conversation
from .gif import gif_note
from .hermes import HermesError
from .memory import with_notes

logger = logging.getLogger(__name__)

_GROUP_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP)


def _is_group(chat) -> bool:
    return chat is not None and chat.type in _GROUP_TYPES


async def _body_for(
    svc, message, user, *, is_group: bool, note: str | None = None
) -> tuple[str, str, str]:
    """回傳 (送去 Hermes 嘅文字, scope, 訊息原文)。

    訊息文字會標咗發言者，前面再加上**當前發言者**嘅筆記。

    **筆記係 per-user 嘅**：以 `user_id` + scope 分開，所以同一個群入面
    甲嘅筆記唔會出現喺乙度。Hermes 嗰邊做唔到呢件事（`USER.md` 係全域）。

    `note` 係媒體描述（真 GIF，見 router/gif.py），附喺訊息後面送去 Hermes。
    **但回傳嘅第三個值係原文** —— 抽筆記一定要用原文，唔可以連媒體描述
    一齊抽。大肥鯨嘅血淚規則：`media_note` 唔可以落庫，落咗下一輪歷史就
    多一段，模型會當成自己講過嘅嘢。
    """
    raw = message.text or message.caption or ""
    scope = scope_for(is_group, message.chat_id)
    notes = await svc.sessions.notes(user.id, scope)

    shown = raw
    if note:
        shown = f"{raw}\n\n[這一則嘅內容 {note}]" if raw else f"[這一則嘅內容 {note}]"

    return with_notes(attribute(user.full_name, user.id, shown), notes), scope, raw


def _replies_to_bot(message, bot_id: int) -> bool:
    """呢則係咪引用緊本鯨嘅訊息。"""
    parent = message.reply_to_message
    if parent is None:
        return False
    sender = getattr(parent, "from_user", None)
    return sender is not None and sender.id == bot_id


async def _conversation_for(svc, message) -> str:
    """呢則訊息應該入邊條對話。

    引用本鯨嘅回答 → 續返嗰條；否則開新一條（用呢則自己嘅 id 做名）。
    """
    if _replies_to_bot(message, svc.bot_id):
        existing = await svc.chain.conversation_of(
            message.chat_id, message.reply_to_message.message_id
        )
        if existing:
            return existing
    return group_conversation(message.chat_id, message.message_id)


async def _deliver(
    message,
    bot,
    svc,
    conversation: str,
    text: str,
    *,
    extract: tuple[int, str, str] | None = None,
) -> None:
    """交去 Hermes 跟住回覆，最後補快取自己嗰則。

    **補快取呢步唔可以省，而且要連 `conversation` 一齊寫。**
    Telegram 唔會將 bot 自己發嘅訊息回傳畀 bot；冇補嘅話，別人引用本鯨
    嗰時查唔到對話，嗰句會被當成新串 —— 對話即刻斷。

    `extract` 係 (user_id, scope, 訊息原文) —— 有值就背景抽筆記。
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
            conversation=conversation,
        )

    # 背景抽筆記，唔阻塞回覆。冇 `people` 參數 = 所有事實歸呢位發言者
    # —— router 每次只收到一個人嘅一句，所以唔使大肥鯨嗰套按名歸屬。
    if extract is not None:
        user_id, scope, user_text = extract
        svc.memory.schedule(
            tg_user_id=user_id,
            scope=scope,
            user_text=user_text,
            assistant_text=reply.text,
        )


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """群組：被 @ 或引用本鯨 → 交去對應嘅 Hermes 對話。"""
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

    # Bot API 收唔到其他 bot 嘅訊息，所以被引用嗰則若唔喺快取就用即時更新補上
    # —— 否則查唔到佢屬於邊條對話。
    if message.reply_to_message is not None:
        await svc.chain.cache_from_update(message.reply_to_message)

    conversation = await _conversation_for(svc, message)
    # 真 GIF 由 Router 自己睇（Hermes 收唔到 —— 見 router/gif.py）。
    note = await gif_note(context.bot, message, svc)
    body, scope, raw = await _body_for(svc, message, user, is_group=True, note=note)

    await _deliver(
        message,
        context.bot,
        svc,
        conversation,
        body,
        extract=(user.id, scope, raw),
    )


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
    note = await gif_note(context.bot, message, svc)
    body, scope, raw = await _body_for(svc, message, user, is_group=False, note=note)

    await _deliver(
        message,
        context.bot,
        svc,
        conversation,
        body,
        extract=(user.id, scope, raw),
    )