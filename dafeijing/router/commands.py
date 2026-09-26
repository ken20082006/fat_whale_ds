"""Router 嘅指令。

**大部分直接複用大肥鯨嗰啲** —— 佢哋只靠 `get_services(context)`、`svc.access`、
`svc.sessions`、`svc.db`，全部 Router 都有。借過嚟用比抄一份好：兩邊行為
一致，而且日後改一處兩邊都受惠。

有三類分開處理：

1. **直接借**：`/start`（邀請碼）、`/remember`、`/forget`、`/id`，
   同管理員嗰批（`/issue`、`/revoke`、`/invites`、`/allowgroup`、
   `/denygroup`、`/groups`、`/block`、`/unblock`）
2. **改寫**：`/help`、`/context`、`/new` —— 大肥鯨嗰版本講嘅嘢 Router 冇
   （思考開關、聯網模式、session 摘要），照搬會誤導
3. **唔適用，唔註冊**：`/tune`、`/reload_persona`、`/undo`、`/export`、
   `/vibe`、`/think`、`/search` —— 嗰啲控制大肥鯨自己個腦，
   而家個腦係 Hermes（人設喺 SOUL.md，調參喺 Hermes 嘅 config）

`/cost`、`/quota`、`/stats` 註冊咗，但**成本係估算** —— Hermes 嘅 API
只回 tokens 唔回 cost（準確數字喺 Hermes 自己個 `state.db`，但讀佢內部
DB 太脆弱）。價錢喺 `settings.py` 嘅 `hermes_*_price`，換模型要跟住改。
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import Application, CommandHandler, ContextTypes

# 直接借大肥鯨嗰批。裝飾器亦係 —— 佢哋只靠 get_services 同 svc.access。
from ..bot.commands import (
    cmd_allowgroup,
    cmd_block,
    cmd_cost,
    cmd_denygroup,
    cmd_forget,
    cmd_groups,
    cmd_id,
    cmd_invites,
    cmd_issue,
    cmd_quota,
    cmd_remember,
    cmd_revoke,
    cmd_start,
    cmd_unblock,
    ensure_active,
)

logger = logging.getLogger(__name__)

_USER_HELP = (
    "本鯨識呢幾樣：\n"
    "\n"
    "  /new — 開新對話（私聊）\n"
    "  /context — 睇下本鯨記得你啲乜\n"
    "  /remember <嘢> — 叫本鯨記住一件事\n"
    "  /forget — 清走筆記（加 all 清晒全部場合）\n"
    "  /id — 睇你嘅 user id\n"
    "  /help — 呢個表\n"
    "\n"
    "直接講嘢就得，唔使指令。@ 本鯨或者回覆本鯨嘅訊息就得。"
)

_ADMIN_HELP = (
    "\n管理員\n"
    "  /issue <備註> — 產生邀請碼，5 分鐘內有效\n"
    "  /revoke <碼> — 撤銷未用嘅邀請碼\n"
    "  /invites — 列出邀請碼狀態\n"
    "  /allowgroup [chat_id] — 永久放行群組\n"
    "  /denygroup [chat_id] — 取消永久放行\n"
    "  /groups — 列出群組同授權狀態\n"
    "  /block <user_id> — 停用使用者\n"
    "  /unblock <user_id> — 解除停用"
)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Router 版：只列真係有嘅指令。"""
    from ..bot.commands import get_services

    svc = get_services(context)
    user = update.effective_user
    text = _USER_HELP
    if user is not None and svc.is_admin(user.id):
        text += _ADMIN_HELP
    await update.effective_message.reply_text(text)


async def cmd_context(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Router 版：講返筆記，唔講 session 摘要（嗰啲交咗畀 Hermes）。"""
    if not await ensure_active(update, context):
        return
    from ..bot.commands import get_services

    svc = get_services(context)
    user = update.effective_user
    if user is None:
        return

    counts = await svc.sessions.note_counts(user.id)
    if not counts:
        body = "本鯨而家冇你嘅筆記。"
    else:
        lines = [f"  {scope} — {n} 則" for scope, n in counts]
        body = "本鯨記得你嘅嘢：\n" + "\n".join(lines)

    await update.effective_message.reply_text(
        body
        + "\n\n（對話歷史唔喺呢度 —— 每條引用串係一條獨立對話，"
        "由 Hermes 管。）"
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """開新對話。

    只有私聊有意義 —— 群組嗰邊，每條引用串本來就係一條獨立對話，
    唔引用任何嘢 @ 本鯨就已經開新嘅。所以喺群組叫呢個指令會講清楚。
    """
    if not await ensure_active(update, context):
        return
    from ..bot.commands import get_services

    svc = get_services(context)
    chat = update.effective_chat
    message = update.effective_message

    if chat.type != ChatType.PRIVATE:
        await message.reply_text(
            "群組唔使呢個指令 —— 唔引用任何嘢 @ 本鯨就已經開新對話。\n"
            "想續返之前嗰條，就引用本鯨嘅回覆。"
        )
        return

    # 私聊嘅對話名加一個序號，令佢接唔返上一條。
    svc.dm_generation = getattr(svc, "dm_generation", 0) + 1
    await message.reply_text("好，開新嘅。之前傾過嘅本鯨照樣記得。")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Router 版。大肥鯨嗰版本報「攔下的洩漏」「聯網搜尋」「讀取連結」——
    嗰啲係大肥鯨自己個腦做嘅嘢，而家喺 Hermes 嗰邊，呢度冇數可以報。

    所以只報 Router 真正知嘅：授權人數、群組快取、錯誤、運行時間。
    """
    import time

    from ..bot.commands import admin_only, get_services

    @admin_only
    async def _run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        svc = get_services(context)
        active = await svc.db.fetchval(
            "SELECT COUNT(*) FROM users WHERE status = 'active'", default=0
        )
        pending = await svc.db.fetchval(
            "SELECT COUNT(*) FROM users WHERE status = 'pending'", default=0
        )
        cached = await svc.db.fetchval("SELECT COUNT(*) FROM group_cache", default=0)
        profiles = await svc.db.fetchval("SELECT COUNT(*) FROM group_profile", default=0)
        stickers = len(svc.stickers.menu().splitlines()) if svc.stickers.available else 0
        uptime = int(time.time() - svc.started_at) if svc.started_at else 0

        lines = [
            "Router 運轉狀態：",
            f"　已授權使用者　{active}",
            f"　待驗證　　　　{pending}",
            f"　群組快取　　　{cached} 則",
            f"　群組概況　　　{profiles} 個群",
            f"　精選貼圖　　　{stickers} 張",
            f"　累計錯誤　　　{svc.errors}",
            f"　運行時間　　　{uptime // 3600} 小時 {uptime % 3600 // 60} 分",
        ]
        await update.effective_message.reply_text("\n".join(lines))

    await _run(update, context)


def register(application: Application) -> None:
    """掛上所有 Router 支援嘅指令。"""
    user_commands = (
        ("start", cmd_start),
        ("help", cmd_help),
        ("new", cmd_new),
        ("context", cmd_context),
        ("remember", cmd_remember),
        ("forget", cmd_forget),
        ("quota", cmd_quota),
        ("id", cmd_id),
    )
    admin_commands = (
        ("issue", cmd_issue),
        ("revoke", cmd_revoke),
        ("invites", cmd_invites),
        ("allowgroup", cmd_allowgroup),
        ("denygroup", cmd_denygroup),
        ("groups", cmd_groups),
        ("block", cmd_block),
        ("unblock", cmd_unblock),
        ("cost", cmd_cost),
        ("stats", cmd_stats),
    )
    for name, handler in (*user_commands, *admin_commands):
        application.add_handler(CommandHandler(name, handler))
    logger.info(
        "指令註冊：%d 個使用者、%d 個管理員",
        len(user_commands),
        len(admin_commands),
    )