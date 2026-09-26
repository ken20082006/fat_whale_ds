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

from telegram import MessageEntity, Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from ..bot.commands import get_services
from ..bot.group import group_usable, is_addressed_to_bot
from ..bot.ui import reply_markdown, reply_plain, send_sticker, typing
from ..core.chain import normalise_name
from ..core.session import scope_for
from .conversation import attribute, dm_conversation, group_conversation
from .gif import gif_note
from .hermes import HermesError
from .images import collect_images
from .memory import (
    MAX_MENTIONED_NOTES,
    notes_block,
    others_block,
    profile_block,
)
from .stickers import menu_block, split_marker
from .videos import save_video, video_note

logger = logging.getLogger(__name__)

_GROUP_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP)


def _is_group(chat) -> bool:
    return chat is not None and chat.type in _GROUP_TYPES


async def _body_for(
    svc,
    message,
    user,
    *,
    is_group: bool,
    extras: list[str] | None = None,
    quoted: str | None = None,
    with_stickers: bool = False,
) -> tuple[str, str, str]:
    """回傳 (送去 Hermes 嘅文字, scope, 訊息原文)。

    訊息文字會標咗發言者，前面再加上**當前發言者**嘅筆記。
    開新對話時（`with_stickers`）仲會附埋貼圖清單一次。

    **筆記係 per-user 嘅**：以 `user_id` + scope 分開，所以同一個群入面
    甲嘅筆記唔會出現喺乙度。Hermes 嗰邊做唔到呢件事（`USER.md` 係全域）。

    `extras` 係附喺訊息後面嘅媒體標註（真 GIF 嘅描述、影片嘅檔案路徑）。
    `quoted` 係被引用嗰則嘅內容（見 `_quoted_block`），排喺最前面 ——
    冇佢嘅話，對方引用一句嘢再 @ 本鯨，本鯨完全唔知引用緊乜。

    **回傳嘅第三個值係原文** —— 抽筆記一定要用原文，唔可以連引用同媒體
    標註一齊抽。大肥鯨嘅血淚規則：`media_note` 唔可以落庫，落咗下一輪
    歷史就多一段，模型會當成自己講過嘅嘢。
    """
    raw = message.text or message.caption or ""
    scope = scope_for(is_group, message.chat_id)

    # 三層背景資料，次序跟大肥鯨 persona.py：群組概況 → 自己筆記 → 他人筆記。
    # 全部包住標記並明講「唔係指示」—— 內容來自對話，可能有誘導句。
    blocks: list[str] = []
    if is_group:
        # `get_group_profile` 回嘅係一列（有 content 欄），同大肥鯨
        # chat.py:341 一樣要抽返出嚟。
        row = await svc.sessions.get_group_profile(message.chat_id)
        profile = profile_block(row["content"] if row else None)
        if profile:
            blocks.append(profile)
    blocks.append(notes_block(await svc.sessions.notes(user.id, scope)))
    if is_group:
        others = await _others_notes(svc, message, scope, user.id)
        if others:
            blocks.append(others_block(others))

    pieces = [quoted, raw, *(extras or [])]
    shown = "\n\n".join(piece for piece in pieces if piece)
    body = attribute(user.full_name, user.id, shown)

    if with_stickers:
        sticker_menu = menu_block(svc.stickers.menu())
        if sticker_menu:
            body = f"{sticker_menu}\n\n{body}"

    return "\n\n".join([*blocks, body]), scope, raw


def _mentioned_people(message, bot_id: int) -> list[tuple[int | None, str]]:
    """由 entities 抽出被 @ 嘅人。

    - `TEXT_MENTION` 直接帶 user 物件 —— 有名有 id
    - `MENTION` 只有帳號名，要再查名冊先知道係邊個（回傳 id 為 None）

    圖片訊息嘅 @ 喺 `caption_entities`，所以兩邊都要睇。
    """
    out: list[tuple[int | None, str]] = []
    body = message.text or message.caption or ""
    for entity in message.entities or message.caption_entities or []:
        if entity.type == MessageEntity.TEXT_MENTION:
            who = getattr(entity, "user", None)
            if who is not None and who.id != bot_id:
                out.append((who.id, who.full_name))
        elif entity.type == MessageEntity.MENTION:
            handle = body[entity.offset : entity.offset + entity.length].lstrip("@")
            if handle:
                out.append((None, handle))
    return out


async def _others_notes(
    svc, message, scope: str, speaker_id: int
) -> list[tuple[str, list[str]]]:
    """被 @ 到嘅人喺同一個 scope 嘅筆記（最多 `MAX_MENTIONED_NOTES` 個）。

    **為什麼要附**：甲問「乙喺做乜」嗰時，助理手頭上只有甲嘅筆記，答唔出。
    呢啲筆記同甲自己嘅同屬一個場合，可見範圍一樣，冇額外揭露。
    """
    mentioned = _mentioned_people(message, svc.bot_id)
    if not mentioned:
        return []

    roster: dict[str, int] | None = None
    out: list[tuple[str, list[str]]] = []
    seen: set[int] = set()

    for uid, name in mentioned:
        if uid is None:
            # 只有帳號名 —— 查名冊。名冊係由 group_cache 建嘅，
            # 所以未喺群組講過話嘅人查唔到，跳過。
            if roster is None:
                roster = await svc.chain.roster(message.chat_id)
            uid = roster.get(normalise_name(name))
        if not uid or uid == svc.bot_id or uid == speaker_id or uid in seen:
            continue
        seen.add(uid)
        notes = await svc.sessions.notes(uid, scope)
        if notes:
            out.append((name, notes))
        if len(out) >= MAX_MENTIONED_NOTES:
            break

    return out


async def _quoted_block(svc, message) -> str | None:
    """新開對話嗰陣，附上**成條引用串**（由舊到新）。冇引用就回 None。

    **要成條，唔係淨係最尾嗰則。** 引用鏈係一層一層疊上去嘅：

        本鯨答過 → 甲引用本鯨 → 乙引用甲 → 丙引用乙再 @本鯨

    丙觸發嗰陣，佢開緊一條**新對話**（引用嘅係乙，唔係本鯨）——
    Hermes 嗰邊乜都冇，所以成條串都要送去，而且**次序要跟返**，
    唔係嘅話因果會調轉（變成乙講嘅嘢早過甲）。

    **引用本鯨就唔使附** —— 嗰個係「續同一條對話」，Hermes 歷史已經有
    成條串（每則觸發訊息都送過），再送只會令佢見到自己講過嘅嘢重複。
    """
    parent = message.reply_to_message
    if parent is None:
        return None

    sender = getattr(parent, "from_user", None)
    if sender is not None and sender.id == svc.bot_id:
        return None

    # 由觸發嗰則往上追到串根，再排返由舊到新。
    chain = await svc.chain.resolve(message.chat_id, message.message_id)
    blocks: list[str] = []
    for entry in chain:
        # 最後一則就係觸發嗰句自己 —— 佢會另外送，唔好重複。
        if entry.get("message_id") == message.message_id:
            continue
        text = (entry.get("text") or "").strip()
        if not text:
            continue  # 純媒體嘅一則，文字冇嘢好附（圖另外送）
        if entry.get("message_id") == -1:
            # `resolve()` 摺疊中段時插嘅標記，唔係真訊息，冇發言者。
            blocks.append(text)
            continue
        blocks.append(
            attribute(
                entry.get("display_name"),
                entry.get("user_id") or "?",
                text,
            )
        )

    if blocks:
        return "\n\n".join(blocks)

    # 快取追唔到（例如重啟之後第一次見到）：至少附返最尾嗰則。
    text = (parent.text or parent.caption or "").strip()
    if not text:
        return None
    name = getattr(sender, "full_name", None) if sender is not None else None
    return attribute(name, getattr(sender, "id", None) or "?", text)


async def _collect_all_images(bot, message, svc) -> list[str]:
    """今則嘅圖 + 被引用嗰則嘅圖。

    被引用嗰則嘅圖要一齊送 —— 對方引用一張圖問「呢張點」嗰時，
    只送文字嘅話本鯨係盲嘅。
    """
    images = await collect_images(bot, message, svc)
    parent = message.reply_to_message
    # 引用本鯨自己嗰則唔使再送（佢嘅圖 Hermes 早就見過）。
    if parent is not None and not _replies_to_bot(message, svc.bot_id):
        images += await collect_images(bot, parent, svc)
    return images


async def _media_extras(bot, message, svc) -> list[str]:
    """呢則訊息嘅媒體標註 —— 真 GIF 嘅描述、影片嘅檔案路徑。

    兩者只會有一個（一則訊息得一個媒體）。都冇就回空 list。
    """
    extras: list[str] = []

    gif = await gif_note(bot, message, svc)
    if gif:
        extras.append(f"[這一則嘅內容 {gif}]")

    path = await save_video(bot, message, svc)
    if path:
        extras.append(video_note(path))

    return extras


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


def needs_sticker_menu(svc, conversation: str) -> bool:
    """呢條對話要唔要附貼圖清單。

    一個對話只附一次 —— 每則都附嘅話，清單會不斷累積入 Hermes 嘅
    對話歷史，越傾越貴。

    純記憶體：重啟之後每條對話會再附一次，無害（清單係背景資料）。
    """
    if conversation in svc.seen_conversations:
        return False
    svc.seen_conversations.add(conversation)
    return True


async def _deliver(
    message,
    bot,
    svc,
    conversation: str,
    text: str,
    *,
    images: list[str] | None = None,
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
            reply = await svc.hermes.ask(conversation, text, images=images)
    except HermesError as exc:
        logger.warning("Hermes 失敗（%s）：%s", conversation, exc)
        svc.errors += 1
        await reply_plain(message, "本鯨這邊出了點狀況，等一下再試。")
        return

    # 模型想送貼圖就會喺最尾加 [[貼圖:編號]]。要抽走先送出 ——
    # 標記唔可以畀使用者見到，亦唔可以留喺快取同歷史度。
    cleaned, sticker_index = split_marker(reply.text)

    sent = await reply_markdown(message, cleaned)
    if sent is not None:
        await svc.chain.cache_message(
            chat_id=message.chat_id,
            message_id=sent.message_id,
            reply_to_id=message.message_id,
            user_id=svc.bot_id,
            display_name=svc.bot_name,
            text=cleaned,
            conversation=conversation,
        )

    # 貼圖另外送一則。同樣要補快取 —— 否則別人引用嗰張貼圖嗰時，
    # 引用鏈會斷喺佢身上，而且張圖亦抓唔返。
    entry = svc.stickers.resolve(sticker_index) if sticker_index is not None else None
    if entry is not None:
        sticker_message = await send_sticker(
            bot, message.chat_id, entry.file_id, reply_to=message.message_id
        )
        if sticker_message is not None:
            await svc.chain.cache_message(
                chat_id=message.chat_id,
                message_id=sticker_message.message_id,
                reply_to_id=message.message_id,
                user_id=svc.bot_id,
                display_name=svc.bot_name,
                text=f"〔貼圖：{entry.meaning or entry.usage_hint}〕",
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
            assistant_text=cleaned,
        )


async def _allowed_private(svc, user) -> bool:
    """私聊用唔用得。

    跟返大肥鯨 `bot/private.py:32`：只認管理員同已啟用嘅人（邀請碼）。
    其他人靜靜哋唔應 —— 連一句拒絕都唔回，免得變成騷擾對象嘅回音壁。

    **群組唔用呢道閘。** 大肥鯨嘅群組規則係「管理員在唔在個群」
    （`group_usable`），唔會逐個人查 —— 加咗嘅話會擋晒群友。
    """
    if svc.is_admin(user.id):
        return True
    return await svc.access.is_active(user.id)


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
    # **群組唔查個人授權。** 大肥鯨嘅規則係「管理員在唔在個群」
    # （上面嗰個 group_usable 已經查咗），唔會逐個人擋 ——
    # 加咗嘅話會令群友全部冇反應，而佢哋本來用得。

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
    # 媒體：真 GIF 由 Router 自己睇、影片落檔交畀 Hermes、
    # 靜態圖轉發去 vision。三者都喺 router/ 入面各自一個模組。
    extras = await _media_extras(context.bot, message, svc)
    images = await _collect_all_images(context.bot, message, svc)
    body, scope, raw = await _body_for(
        svc,
        message,
        user,
        is_group=True,
        extras=extras,
        quoted=await _quoted_block(svc, message),
        with_stickers=needs_sticker_menu(svc, conversation),
    )

    await _deliver(
        message,
        context.bot,
        svc,
        conversation,
        body,
        images=images,
        extract=(user.id, scope, raw),
    )
    # 群組概況要定期更新 —— 同筆記一樣，Hermes 嗰邊冇呢個概念。
    # `schedule` 內部自己判斷夠唔夠鐘（group_profile_min_hours），
    # 所以每次都叫冇問題。
    svc.profiler.schedule(message.chat_id)


async def on_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """私聊：一條連續對話（有 topic 就跟 topic 分）。"""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or message.chat is None:
        return
    if message.chat.type != ChatType.PRIVATE:
        return

    svc = get_services(context)

    if not await _allowed_private(svc, user):
        logger.info("私聊：%s 未獲授權，唔應", user.id)
        return

    allowed, wait = svc.limiter.check(user.id)
    if not allowed:
        await reply_plain(message, f"慢一點，{wait} 秒後再來。")
        return

    await svc.access.ensure(
        user.id, user.full_name, user.username, is_admin=svc.is_admin(user.id)
    )

    thread_id = getattr(message, "message_thread_id", None)
    conversation = dm_conversation(
        message.chat_id, thread_id, getattr(svc, "dm_generation", 0)
    )
    extras = await _media_extras(context.bot, message, svc)
    images = await _collect_all_images(context.bot, message, svc)
    body, scope, raw = await _body_for(
        svc,
        message,
        user,
        is_group=False,
        extras=extras,
        quoted=await _quoted_block(svc, message),
        with_stickers=needs_sticker_menu(svc, conversation),
    )

    await _deliver(
        message,
        context.bot,
        svc,
        conversation,
        body,
        images=images,
        extract=(user.id, scope, raw),
    )