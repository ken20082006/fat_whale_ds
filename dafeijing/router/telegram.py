"""Router 由舊版 Telegram 層搬過嚟嘅幾件嘢。

原本係 `from ..bot.group import …` 借返嚟。但 `bot/group.py` 為咗舊版嘅
`handle_group_trigger`，import 咗成個舊腦 —— `core/chat`、`core/debounce`、
`llm/openrouter`、`bot/ingest` —— 而 Router 一行都冇叫過嗰啲。
依賴分析睇得出「reachable」，但純粹係 import 鏈拖住。

搬入嚟之後 `bot/` 可以整個刪走，`core/chat.py`、`core/security.py`、
`core/webfetch.py` 亦跟住死。

**呢四件係真嘅共用邏輯，唔係舊嘢**：群組可用性（管理員在唔在場）、
訊息快取（引用鏈嘅原材料）、被指名判定。
"""

from __future__ import annotations

import logging

from telegram import MessageEntity, Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from .services import RouterServices

logger = logging.getLogger(__name__)




def get_services(context: ContextTypes.DEFAULT_TYPE) -> Services:
    return context.bot_data["services"]



# PTB 的 ChatMember.status 是字串
_PRESENT_STATUSES = frozenset({"creator", "administrator", "member", "restricted"})




def _is_group(chat) -> bool:
    return chat is not None and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)




async def _admin_present(bot, chat_id: int, admin_ids: set[int]) -> bool:
    """逐一查管理員是否在群組裡。任何一位在就算數。"""
    for admin_id in admin_ids:
        try:
            member = await bot.get_chat_member(chat_id, admin_id)
        except Exception as exc:
            logger.debug("查不到 %s 在群組 %s 的身分：%s", admin_id, chat_id, exc)
            continue
        if member.status in _PRESENT_STATUSES:
            return True
    return False




async def group_usable(svc: Services, bot, chat_id: int) -> bool:
    """這個群組能不能用。手動白名單優先，其次看管理員在不在。"""
    if await svc.access.is_group_allowed(chat_id):
        return True
    if not svc.cfg.admin_ids:
        return False

    cached = svc.group_access.get(chat_id)
    if cached is not None:
        return cached

    present = await _admin_present(bot, chat_id, svc.cfg.admin_ids)
    svc.group_access.put(chat_id, present)
    return present




# ── 快取 ────────────────────────────────────────────────


async def cache_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """每個群組訊息都入快取。這是以隱私換取引用鏈回溯的能力。"""
    message = update.effective_message
    if message is None or message.chat is None:
        return

    svc = get_services(context)
    if not await group_usable(svc, context.bot, message.chat_id):
        return

    await svc.chain.cache_from_update(message)




def is_addressed_to_bot(message, svc: Services) -> bool:
    """被 @，或是回覆本鯨的訊息。圖片訊息要看 caption_entities。"""
    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.id == svc.bot_id:
            return True

    if message.via_bot and message.via_bot.id == svc.bot_id:
        return True

    if not svc.bot_username:
        return False

    body = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    target = svc.bot_username.lower()

    for entity in entities:
        if entity.type != MessageEntity.MENTION:
            continue
        mentioned = body[entity.offset : entity.offset + entity.length]
        if mentioned.lstrip("@").lower() == target:
            return True
    return False
