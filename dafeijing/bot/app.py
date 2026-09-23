"""組裝 Telegram Application。"""

from __future__ import annotations

import asyncio
import logging
import time

from telegram import Update
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..core.access import AccessControl, MembershipCache
from ..core.chat import ChatService
from ..core.chain import ReplyChain
from ..core.debounce import Debouncer
from ..core.memory import MemoryExtractor
from ..core.persona import Persona
from ..core.ratelimit import RateLimiter
from ..core.session import SessionManager
from ..core.stickers import StickerLibrary
from ..core.usage import UsageLog
from ..llm.openrouter import OpenRouterClient
from ..settings import Settings
from ..store.db import Database
from . import commands, group, private
from .services import Services

logger = logging.getLogger(__name__)

PURGE_INTERVAL_SECONDS = 60 * 60


def create_services(cfg: Settings) -> Services:
    db = Database(cfg.db_path)
    access = AccessControl(db)
    sessions = SessionManager(db, cfg)
    chain = ReplyChain(db, cfg)
    persona = Persona.load(cfg.persona_file)
    llm = OpenRouterClient(cfg)
    usage = UsageLog(db)
    memory = MemoryExtractor(cfg, sessions, llm)
    stickers = StickerLibrary(cfg)

    return Services(
        cfg=cfg,
        db=db,
        access=access,
        sessions=sessions,
        chain=chain,
        persona=persona,
        llm=llm,
        usage=usage,
        chat=ChatService(cfg, persona, sessions, access, llm, usage, memory, stickers),
        memory=memory,
        stickers=stickers,
        debouncer=Debouncer(cfg.debounce_seconds),
        limiter=RateLimiter(cfg.rate_per_minute),
        group_access=MembershipCache(ttl_seconds=cfg.group_membership_ttl_seconds),
        started_at=time.time(),
    )


def build_application(svc: Services) -> Application:
    application = (
        Application.builder()
        .token(svc.cfg.telegram_bot_token)
        # 預設逾時偏短，家用網路偶爾一個抖動就讓 bot 起不來。
        # 啟動階段的 getMe 由 PTB 自己做，我們的重試涵蓋不到。
        .connect_timeout(20.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(10.0)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    application.bot_data["services"] = svc

    # ── 群組訊息快取：必須最先跑，且不阻擋後續 handler ──
    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL,
            group.cache_group_message,
        ),
        group=-1,
    )

    # ── 進出群組 ──
    application.add_handler(
        ChatMemberHandler(group.on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER)
    )
    # 追蹤管理員的進出，這是群組可用性的依據
    application.add_handler(
        ChatMemberHandler(group.on_chat_member, ChatMemberHandler.CHAT_MEMBER)
    )

    # ── 使用者指令 ──
    for name, handler in (
        ("start", commands.cmd_start),
        ("help", commands.cmd_help),
        ("new", commands.cmd_new),
        ("reset", commands.cmd_reset),
        ("undo", commands.cmd_undo),
        ("context", commands.cmd_context),
        ("vibe", commands.cmd_vibe),
        ("think", commands.cmd_think),
        ("search", commands.cmd_search),
        ("remember", commands.cmd_remember),
        ("forget", commands.cmd_forget),
        ("export", commands.cmd_export),
        ("quota", commands.cmd_quota),
        ("id", commands.cmd_id),
    ):
        application.add_handler(CommandHandler(name, handler))

    # ── 管理員指令 ──
    for name, handler in (
        ("issue", commands.cmd_issue),
        ("revoke", commands.cmd_revoke),
        ("invites", commands.cmd_invites),
        ("allowgroup", commands.cmd_allowgroup),
        ("denygroup", commands.cmd_denygroup),
        ("groups", commands.cmd_groups),
        ("cost", commands.cmd_cost),
        ("stats", commands.cmd_stats),
        ("block", commands.cmd_block),
        ("unblock", commands.cmd_unblock),
        ("reload_persona", commands.cmd_reload),
    ):
        application.add_handler(CommandHandler(name, handler))

    # ── 一般訊息（文字、圖片、貼圖都走同一條路）──
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND,
            private.handle_private_message,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & ~filters.COMMAND & ~filters.StatusUpdate.ALL,
            group.handle_group_trigger,
        )
    )

    application.add_error_handler(_on_error)

    if application.job_queue is not None:
        application.job_queue.run_repeating(
            _purge_job, interval=PURGE_INTERVAL_SECONDS, first=120, name="purge"
        )
    else:
        logger.warning("沒有 job_queue，過期清理不會自動執行（請安裝 job-queue 擴充）")

    return application


# ── 生命週期 ────────────────────────────────────────────


async def _post_init(application: Application) -> None:
    svc: Services = application.bot_data["services"]
    await svc.db.connect()
    await svc.stickers.load(svc.db)

    # 啟動時一次網路抖動就會讓整個 bot 起不來，所以重試。
    # get_me 失敗沒有什麼可補救的，但不該因此開不了機。
    me = None
    for attempt in range(5):
        try:
            me = await application.bot.get_me()
            break
        except Exception as exc:
            logger.warning("取得 bot 資訊失敗（第 %d/5 次）：%s", attempt + 1, exc)
            await asyncio.sleep(2.0 * (attempt + 1))

    if me is None:
        raise RuntimeError("連不上 Telegram，無法啟動")

    svc.bot_username = me.username or ""
    svc.bot_id = me.id
    svc.bot_name = me.first_name or "大肥鯨"

    logger.info(
        "大肥鯨上線：@%s（id=%s）｜模型 %s｜管理員 %s",
        svc.bot_username,
        svc.bot_id,
        svc.cfg.model,
        sorted(svc.cfg.admin_ids) or "（未設定）",
    )
    if not svc.cfg.admin_ids:
        logger.warning("FW_ADMIN_USER_IDS 是空的，將沒有人能核發邀請碼。先用 /id 查自己的 id。")


async def _post_shutdown(application: Application) -> None:
    svc: Services = application.bot_data.get("services")
    if svc is None:
        return
    logger.info("收工，關閉連線。")
    await svc.debouncer.drain()
    await svc.memory.drain()  # 讓還在跑的記憶抽取寫完，免得白花一次呼叫
    await svc.llm.close()
    await svc.db.close()


# ── 排程 ────────────────────────────────────────────────


async def _purge_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    svc: Services = context.bot_data["services"]
    try:
        counts = await svc.db.purge_expired(
            svc.cfg.history_retention_days, svc.cfg.group_cache_retention_hours
        )
        if counts.get("messages") or counts.get("group_cache"):
            logger.info(
                "定期清理：對話原文 %d 則、群組快取 %d 則",
                counts.get("messages", 0),
                counts.get("group_cache", 0),
            )
    except Exception:
        logger.exception("定期清理失敗")


# ── 錯誤 ────────────────────────────────────────────────


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc: Services | None = context.bot_data.get("services")
    if svc is not None:
        svc.errors += 1

    logger.error("未處理的例外", exc_info=context.error)

    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text("本鯨栽了一下，已經記在日誌裡了。")
        except Exception:
            pass
