"""群組處理。

三件事各自獨立：
1. 進出群組 —— 判斷這個群組能不能用（管理員在不在裡面）
2. 快取 —— 所有群組訊息寫入短期快取，只為了還原引用串，永不送進模型
3. 回應 —— 只有「被 @」或「回覆本鯨的訊息」才觸發，上下文限於該條引用串

群組的可用條件是「管理員目前在這個群組裡」，而不是「誰把本鯨加進來」。
這樣只要主人在，換誰拉本鯨進群都能用；主人一走，本鯨也跟著走。
"""

from __future__ import annotations

import logging

from telegram import MessageEntity, Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from ..core.chain import normalise_name
from ..core.chat import ChatRequest
from ..core.debounce import MAX_IMAGES
from ..core.media import MediaError, Picked, PreparedImage, collect_media
from ..core.memory import Person
from ..llm.openrouter import LLMError
from .commands import get_services
from .ingest import collect
from .services import Services
from .ui import reply_markdown, reply_plain, send_sticker, typing

logger = logging.getLogger(__name__)

# PTB 的 ChatMember.status 是字串
_PRESENT_STATUSES = frozenset({"creator", "administrator", "member", "restricted"})
_GONE_STATUSES = frozenset({"left", "kicked"})


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


# ── 進出群組 ────────────────────────────────────────────


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """本鯨被加入或移出群組。"""
    member = update.my_chat_member
    if member is None or not _is_group(member.chat):
        return

    if member.new_chat_member.status in _GONE_STATUSES:
        logger.info("已離開群組 %s", member.chat.id)
        return

    chat = member.chat
    svc = get_services(context)
    added_by = member.from_user.id if member.from_user else None
    added_by_name = member.from_user.full_name if member.from_user else "未知"

    await svc.access.register_group(chat.id, chat.title, added_by)
    svc.group_access.invalidate(chat.id)

    present = await _admin_present(context.bot, chat.id, svc.cfg.admin_ids)
    svc.group_access.put(chat.id, present)

    if present:
        await context.bot.send_message(
            chat.id, "本鯨進來了。要找本鯨就 @ 我，或回覆本鯨的訊息。"
        )
        logger.info(
            "加入群組「%s」（%s），管理員在場，已放行（由 %s 加入）",
            chat.title,
            chat.id,
            added_by_name,
        )
        return

    logger.warning(
        "被加入群組「%s」（%s）但管理員不在，退出（由 %s 加入）",
        chat.title,
        chat.id,
        added_by_name,
    )
    await context.bot.send_message(chat.id, "本鯨的主子不在這裡，先告退了。")
    await context.bot.leave_chat(chat.id)

    await _notify_admins(
        context,
        svc,
        f"有人把本鯨加入了群組，但你不在裡面，本鯨已退出。\n\n"
        f"　加入者：{added_by_name}（id: {added_by}）\n"
        f"　群組：{chat.title}（id: {chat.id}）\n\n"
        f"想讓本鯨留在那個群，你先進去，再請對方重新加入。"
        f"或用 /allowgroup {chat.id} 永久放行。",
    )


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """追蹤管理員的進出，這是群組可用性的依據。"""
    change = update.chat_member
    if change is None or not _is_group(change.chat):
        return

    svc = get_services(context)
    who = change.new_chat_member.user
    if not svc.is_admin(who.id):
        return

    chat_id = change.chat.id
    svc.group_access.invalidate(chat_id)
    status = change.new_chat_member.status

    if status in _GONE_STATUSES:
        logger.info("管理員 %s 離開群組 %s", who.id, chat_id)
        if not await svc.access.is_group_allowed(chat_id):
            try:
                await context.bot.send_message(chat_id, "本鯨的主子走了，本鯨也該走了。")
                await context.bot.leave_chat(chat_id)
            except Exception:
                logger.debug("退出群組 %s 失敗", chat_id)
    else:
        logger.info("管理員 %s 在群組 %s 中（%s）", who.id, chat_id, status)


async def _notify_admins(context: ContextTypes.DEFAULT_TYPE, svc: Services, text: str) -> None:
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
    if not await group_usable(svc, context.bot, message.chat_id):
        return

    await svc.chain.cache_from_update(message)


# ── 回應 ────────────────────────────────────────────────


async def handle_group_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    svc = get_services(context)

    if message is None or user is None or not _is_group(message.chat):
        return
    if not await group_usable(svc, context.bot, message.chat_id):
        return
    if not _is_addressed_to_bot(message, svc):
        return

    allowed, wait = svc.limiter.check(user.id)
    if not allowed:
        await message.reply_text(f"慢一點，{wait} 秒後再來。")
        return

    # include_reply=False：被引用的那一則由下面的引用串統一處理，避免重複下載
    text, own_images = await collect(message, context.bot, svc.cfg, include_reply=False)
    if text is None and not own_images:
        return

    # 群組使用者也要有帳號列，否則讀不到他們的偏好設定
    await svc.access.ensure(
        user.id, user.full_name, user.username, is_admin=svc.is_admin(user.id)
    )

    # 被引用的那一則若不在快取裡，用即時更新附帶的內容補上。
    #
    # Bot API 明文規定 bot 收不到其他 bot 的訊息，所以別的 bot 講過的話
    # 永遠不會進快取，引用鏈回溯到那裡就斷掉。但「被引用的那一則」會跟著
    # 使用者的訊息一起送上來（reply_to_message），即使那是別的 bot 發的。
    # 補進快取之後，回溯就接得起來，而且以後再被引用也不會斷。
    if message.reply_to_message is not None:
        await svc.chain.cache_from_update(message.reply_to_message)

    # 引用串回溯：從被指名的那則往上追到源頭
    chain = await svc.chain.resolve(message.chat_id, message.message_id)
    root_id = chain[0]["message_id"] if chain else message.message_id
    chain_text = svc.chain.format_for_prompt(chain, svc.bot_name)

    # 引用串解出來才知道有哪些人發言，所以放在這裡
    people, mentioned = await _collect_people(message, chain, svc)

    # 串裡出現過的圖片也要真的抓下來。只給「〔圖片〕」這種文字標註的話，
    # 對方引用的圖等於沒被看到。
    chain_images = await _gather_chain_media(
        chain, context.bot, svc.cfg, skip={message.message_id}, limit=MAX_IMAGES
    )
    images = chain_images + own_images
    if len(images) > MAX_IMAGES:
        images = images[-MAX_IMAGES:]  # 保留最靠近提問的幾張

    # 群組不做 debounce：每條串都是獨立事件，合併反而會混淆發言者
    session = await svc.sessions.group_thread_session(message.chat_id, root_id)

    request = ChatRequest(
        tg_user_id=user.id,
        display_name=user.full_name,
        text=text or "",
        chat_id=message.chat_id,
        session=session,
        is_group=True,
        chain_text=chain_text or None,
        chain_messages=chain,
        images=images,
        people=people,
        mentioned=mentioned,
    )

    try:
        async with typing(context.bot, message.chat_id):
            outcome = await svc.chat.respond(request)
    except LLMError as exc:
        await reply_plain(message, str(exc))
        return
    except Exception:
        logger.exception("群組回覆失敗")
        svc.errors += 1
        await reply_plain(message, "本鯨這邊出了點狀況。")
        return

    sent = await reply_markdown(message, outcome.text)
    if sent is not None:
        # 本鯨自己的訊息不會從 Telegram 收到，必須自己補進快取，
        # 否則別人回覆本鯨時，引用鏈會在這裡斷掉。
        await svc.chain.cache_message(
            chat_id=message.chat_id,
            message_id=sent.message_id,
            reply_to_id=message.message_id,
            user_id=svc.bot_id,
            display_name=svc.bot_name,
            text=outcome.text,
        )

    # 貼圖另外送一則。同樣要補進快取 —— 否則別人回覆那張貼圖時，
    # 引用鏈會斷在它身上，而且圖也抓不回來。
    if outcome.sticker_file_id:
        sticker_message = await send_sticker(
            context.bot,
            message.chat_id,
            outcome.sticker_file_id,
            reply_to=message.message_id,
        )
        if sticker_message is not None:
            await svc.chain.cache_message(
                chat_id=message.chat_id,
                message_id=sticker_message.message_id,
                reply_to_id=message.message_id,
                user_id=svc.bot_id,
                display_name=svc.bot_name,
                text="〔貼圖〕",
                has_media=True,
                media_file_id=outcome.sticker_file_id,
                media_source="sticker",
            )


async def _collect_people(
    message, chain: list[dict], svc: Services
) -> tuple[list[Person], list[Person]]:
    """回傳 (這一串出現過的人, 這則訊息 @ 到的人)。

    名單必須包含「被提到但還沒發言」的人 —— 甲可以 @ 一個沒講過話的乙，
    然後開始講關於乙的事，那些事實要記在乙頭上而不是甲。
    """
    people: dict[int, str] = {}
    mentioned: dict[int, str] = {}

    # 先看這一串有哪些人發言
    for item in chain:
        user_id = item.get("user_id")
        name = (item.get("display_name") or "").strip()
        if user_id and name and user_id != svc.bot_id:
            people.setdefault(user_id, name)

    # 再看這則訊息 @ 了誰。已在上面的不會被覆蓋，以實際發言的名字為準。
    handles = _mentioned_names(message, svc.bot_username)
    if handles:
        roster = await svc.chain.roster(message.chat_id)
        for handle in handles:
            user_id = roster.get(normalise_name(handle))
            if not user_id or user_id == svc.bot_id:
                continue
            name = people.get(user_id, handle)
            people.setdefault(user_id, name)
            mentioned[user_id] = name

    return (
        [Person(user_id=uid, name=name) for uid, name in people.items()],
        [Person(user_id=uid, name=name) for uid, name in mentioned.items()],
    )


def _mentioned_names(message, bot_username: str) -> list[str]:
    """取出這則訊息 @ 到的名字。

    MENTION 只給帳號名，要再查名冊才知道是誰；TEXT_MENTION 直接帶 user 物件。
    """
    body = message.text or message.caption or ""
    target = (bot_username or "").lower()
    names: list[str] = []

    for entity in message.entities or message.caption_entities or []:
        if entity.type == MessageEntity.TEXT_MENTION and entity.user is not None:
            label = entity.user.full_name or entity.user.username or ""
            if label:
                names.append(label)
        elif entity.type == MessageEntity.MENTION:
            handle = body[entity.offset : entity.offset + entity.length].lstrip("@")
            if handle and handle.lower() != target:
                names.append(handle)

    return names


async def _gather_chain_media(
    chain: list[dict],
    bot,
    cfg,
    *,
    skip: set[int],
    limit: int,
) -> list[PreparedImage]:
    """把引用串裡出現過的圖片抓下來，由舊到新回傳。

    先從最新往回取 —— 越靠近提問的越相關，超過上限時先丟掉最舊的。
    """
    candidates: list[tuple[str, str]] = []

    for item in reversed(chain):
        if len(candidates) >= limit:
            break
        file_id = item.get("media_file_id")
        if not file_id or item.get("message_id") in skip:
            continue
        candidates.append((file_id, item.get("media_source") or "photo"))

    images: list[PreparedImage] = []
    for file_id, source in reversed(candidates):
        try:
            # 快取只存 file_id 與來源，沒有影片的長度與大小，所以影片在這裡
            # 一律只有一張縮圖。真 GIF 因為不需要那些資訊，仍然可以逐格抽。
            images.extend(
                await collect_media(bot, Picked(file_id, source, None), cfg)
            )
        except MediaError:
            # 舊檔可能已失效，略過就好，不該讓整則訊息失敗
            logger.warning("引用串裡的圖片抓不到，略過：%s", file_id)

    return images


def _is_addressed_to_bot(message, svc: Services) -> bool:
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
