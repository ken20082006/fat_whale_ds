"""組裝 Router 嘅 Telegram Application。

對照大肥鯨嘅 `bot/app.py` —— 形狀一樣，但 handler 少好多：
冇指令、冇貼圖、冇記憶抽取、冇定時整理。得三個 handler。
"""

from __future__ import annotations

import asyncio
import logging
import time

from telegram.ext import Application, ContextTypes, MessageHandler, filters

from ..settings import Settings
from .handlers import on_group_message, on_private_message
from .services import RouterServices, create_services

logger = logging.getLogger(__name__)

GET_ME_RETRIES = 5


def build_application(svc: RouterServices, cfg: Settings) -> Application:
    application = (
        Application.builder()
        .token(cfg.telegram_bot_token)
        .connect_timeout(20)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(10)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    application.bot_data["services"] = svc

    # 群組快取行 group=-1（最先跑）。呢個係引用鏈回溯嘅原材料：
    # 每個群組訊息都寫入快取，但只有被指名嘅那條串會送去模型。
    #
    # 直接複用大肥鯨嗰個 handler —— 佢嘅邏輯同 router 完全一樣。
    from ..bot.group import cache_group_message

    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL,
            cache_group_message,
        ),
        group=-1,
    )
    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & ~filters.COMMAND & ~filters.StatusUpdate.ALL,
            on_group_message,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND,
            on_private_message,
        )
    )
    application.add_error_handler(_on_error)
    return application


async def _post_init(application: Application) -> None:
    """Telegram 連上之後才跑 —— 呢度先可以拿 bot 身分。"""
    svc: RouterServices = application.bot_data["services"]
    await svc.db.connect()
    # 要等 db 連上先讀得到 —— 大肥鯨都係喺 post_init 做。
    await svc.stickers.load(svc.db)

    # 啟動時網路抖動唔應該令 bot 開唔到 —— 重試幾次。
    me = None
    for attempt in range(GET_ME_RETRIES):
        try:
            me = await application.bot.get_me()
            break
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_me 第 %d 次失敗：%s", attempt + 1, exc)
            await asyncio.sleep(2 * (attempt + 1))
    if me is None:
        logger.error("連唔上 Telegram —— 檢查 FW_TELEGRAM_BOT_TOKEN")
        return

    svc.bot_username = me.username or ""
    svc.bot_id = me.id
    svc.bot_name = me.first_name or svc.bot_name
    svc.started_at = time.time()

    logger.info(
        "Router 上線：@%s（id=%s）｜Hermes %s｜管理員 %s",
        svc.bot_username,
        svc.bot_id,
        svc.cfg.hermes_url,
        sorted(svc.cfg.admin_ids) or "（未設定）",
    )
    if not svc.cfg.admin_ids:
        logger.warning("FW_ADMIN_USER_IDS 未設定 —— 群組會因為冇管理員在場而停用")


async def _post_shutdown(application: Application) -> None:
    """收工前要等背景抽取做完，否則最後幾則嘅筆記會無聲無息咁丟失。

    次序跟大肥鯨：先 drain 背景工作，再關 client，最後關資料庫 ——
    調轉嘅話抽取會寫落一個已關嘅 db。
    """
    svc: RouterServices = application.bot_data["services"]
    await svc.memory.drain()
    await svc.profiler.drain()
    await svc.llm.close()
    await svc.decisions.close()
    await svc.hermes.close()
    await svc.db.close()


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """群組裡面冇被指名就唔出聲 —— 一個帖文出錯唔應該換嚟一句道歉。

    （大肥鯨都係咁做：曾經有個影片貼圖令 `pick_file()` 拋
    AttributeError，結果群組每個貼圖都換嚟一句「本鯨栽了一下」。）
    """
    logger.error("Router 出事", exc_info=context.error)
    svc: RouterServices | None = None
    try:
        svc = context.application.bot_data.get("services")
    except Exception:  # noqa: BLE001
        pass
    if svc is not None:
        svc.errors += 1

    message = getattr(update, "effective_message", None)
    chat = getattr(message, "chat", None)
    if message is None or chat is None or chat.type != "private":
        return
    from ..bot.ui import reply_plain

    await reply_plain(message, "本鯨這邊出了點狀況，等一下再試。")
