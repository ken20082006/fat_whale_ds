"""使用者指令與管理員指令。"""

from __future__ import annotations

import io
import logging
import time
from functools import wraps

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from ..core.access import RedeemStatus
from ..core.util import humanise_age, truncate
from ..core.tokens import estimate_tokens
from .services import Services

logger = logging.getLogger(__name__)

VALID_VIBES = ("low", "mid", "high")


def get_services(context: ContextTypes.DEFAULT_TYPE) -> Services:
    return context.bot_data["services"]


# ── 裝飾器 ──────────────────────────────────────────────


def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user is None or not get_services(context).is_admin(user.id):
            await update.effective_message.reply_text("這個指令只有管理員能用。")
            return
        return await func(update, context)

    return wrapper


async def ensure_active(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """未授權者一律擋下。回傳是否放行。"""
    svc = get_services(context)
    user = update.effective_user
    if user is None:
        return False
    if svc.is_admin(user.id) or await svc.access.is_active(user.id):
        return True
    await update.effective_message.reply_text(
        f"本鯨只認得被邀請的人。\n請輸入邀請碼，或用邀請連結開啟：\n"
        f"https://t.me/{svc.bot_username}?start=<你的邀請碼>"
    )
    return False


# ── 入口 ────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    payload = context.args[0] if context.args else None

    if payload:
        result = await svc.access.redeem(user.id, payload, user.full_name, user.username)
        replies = {
            RedeemStatus.OK: "邀請碼確認。本鯨記住你了，說吧，要什麼。",
            RedeemStatus.ALREADY_ACTIVE: "你本來就在名單上，不用再刷一次。",
            RedeemStatus.INVALID: "這串碼本鯨不認得。確認一下有沒有打錯。",
            RedeemStatus.REVOKED: "這張邀請碼已經被撤銷了。",
            RedeemStatus.EXPIRED: "這張邀請碼過期了，找邀請你的人再要一張。",
            RedeemStatus.ALREADY_BOUND: "這張邀請碼已經綁給別人了。",
        }
        await message.reply_text(replies.get(result.status, "邀請碼無法使用。"))
        return

    if await svc.access.is_active(user.id) or svc.is_admin(user.id):
        await message.reply_text(
            "又見面了。\n直接說話即可，指令用 /help 看。"
        )
    else:
        await message.reply_text(
            "本鯨是私人養的，不隨便接客。\n"
            "請輸入邀請碼，或直接點邀請連結進來。"
        )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "本鯨聽得懂的指令：\n\n"
        "對話\n"
        "  /new — 開新對話（保留摘要，記憶還在）\n"
        "  /reset — 清空這段對話的記憶\n"
        "  /undo — 收回上一輪，重新問\n"
        "  /context — 看看本鯨現在記得多少\n"
        "  /export — 把這段對話匯出成檔案\n\n"
        "偏好\n"
        "  /vibe low | mid | high — 調整本鯨的演出濃度\n"
        "  /remember <內容> — 要本鯨長期記住這件事\n"
        "  /forget — 清掉長期記憶\n\n"
        "其他\n"
        "  /quota — 查自己的用量\n"
        "  /id — 查自己的 Telegram id\n"
        "  /help — 這份說明\n\n"
        "群組裡要 @ 本鯨，或回覆本鯨的訊息，本鯨才會理你。"
    )
    await update.effective_message.reply_text(text)


# ── 對話控制 ────────────────────────────────────────────


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_active(update, context):
        return
    svc = get_services(context)
    user = update.effective_user
    session = await svc.sessions.private_session(user.id)
    await svc.sessions.soft_reset(session.id, svc.llm.summarise)
    await update.effective_message.reply_text(
        "開新的一頁。之前聊過的重點本鯨還記著，細節忘了。"
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_active(update, context):
        return
    svc = get_services(context)
    session = await svc.sessions.private_session(update.effective_user.id)
    await svc.sessions.hard_reset(session.id)
    await update.effective_message.reply_text(
        "全清了。這段對話本鯨當作沒發生過。\n長期記憶還在，要一起清就用 /forget。"
    )


async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_active(update, context):
        return
    svc = get_services(context)
    session = await svc.sessions.private_session(update.effective_user.id)
    removed = await svc.sessions.undo_last_turn(session.id)
    await update.effective_message.reply_text(
        "收回了，重問一次。" if removed else "沒有東西可以收回。"
    )


async def cmd_context(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_active(update, context):
        return
    svc = get_services(context)
    user = update.effective_user

    if update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text("這個指令請在私聊裡用。")
        return

    session = await svc.sessions.private_session(user.id)
    window = await svc.sessions.window(session.id)
    tokens = await svc.sessions.window_tokens(session.id)
    notes = await svc.sessions.notes(user.id)
    row = await svc.access.get_user(user.id)

    lines = [
        "本鯨現在記得的東西：\n",
        f"短期　{len(window)} 則原文，約 {tokens} token",
        f"中期　摘要 {session.summary_tokens} token",
        f"長期　{len(notes)} 則筆記",
        f"濃度　{row['vibe'] if row else 'mid'}",
        f"上次　{humanise_age(session.last_active_at)}",
    ]
    if session.summary:
        lines.append(f"\n摘要節錄：\n{truncate(session.summary, 300)}")
    if notes:
        lines.append("\n長期筆記：")
        lines.extend(f"  · {truncate(note, 80)}" for note in notes[:8])
        if len(notes) > 8:
            lines.append(f"  （另有 {len(notes) - 8} 則）")

    await update.effective_message.reply_text("\n".join(lines))


async def cmd_vibe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_active(update, context):
        return
    svc = get_services(context)
    user = update.effective_user
    wanted = (context.args[0].lower() if context.args else "").strip()

    if wanted not in VALID_VIBES:
        await update.effective_message.reply_text(
            "用法：/vibe low | mid | high\n"
            "low 最收斂，high 最放飛。預設是 mid。"
        )
        return

    await svc.access.set_vibe(user.id, wanted)
    remarks = {
        "low": "好，本鯨安靜辦事。",
        "mid": "好，照平常那樣。",
        "high": "好，本鯨今天心情不錯。",
    }
    await update.effective_message.reply_text(remarks[wanted])


async def cmd_remember(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_active(update, context):
        return
    svc = get_services(context)
    content = " ".join(context.args).strip() if context.args else ""
    if not content:
        await update.effective_message.reply_text("用法：/remember 你叫什麼、在做什麼之類的。")
        return
    await svc.sessions.add_note(update.effective_user.id, content[:500], source="explicit")
    await update.effective_message.reply_text("記下了。")


async def cmd_forget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_active(update, context):
        return
    svc = get_services(context)
    removed = await svc.sessions.clear_notes(update.effective_user.id)
    await update.effective_message.reply_text(
        f"清掉 {removed} 則筆記。" if removed else "本來就沒有筆記。"
    )


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_active(update, context):
        return
    svc = get_services(context)
    user = update.effective_user

    if update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text("這個指令請在私聊裡用。")
        return

    session = await svc.sessions.private_session(user.id)
    rows = await svc.db.fetchall(
        "SELECT role, content, created_at FROM messages WHERE session_id = ? ORDER BY id",
        (session.id,),
    )
    if not rows:
        await update.effective_message.reply_text("這段對話是空的，沒東西好匯出。")
        return

    lines = [f"# 大肥鯨對話紀錄\n", f"匯出時間：{time.strftime('%Y-%m-%d %H:%M')}\n"]
    if session.summary:
        lines.append(f"\n## 摘要\n\n{session.summary}\n")
    lines.append("\n## 對話\n")
    for row in rows:
        who = "你" if row["role"] == "user" else "大肥鯨"
        lines.append(f"\n**{who}**（{row['created_at']}）\n\n{row['content']}\n")

    payload = io.BytesIO("\n".join(lines).encode("utf-8"))
    payload.name = "fatwhale-chat.md"
    await update.effective_message.reply_document(payload, filename="fatwhale-chat.md")


async def cmd_quota(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_active(update, context):
        return
    svc = get_services(context)
    user = update.effective_user

    today = await svc.usage.user_today(user.id)
    month = await svc.usage.user_summary(user.id, days=30)

    await update.effective_message.reply_text(
        "本鯨今天的食量：\n"
        f"　{today.get('calls', 0)} 次呼叫，{today.get('total_tokens', 0):,} token\n\n"
        "近 30 天：\n"
        f"　{month.get('calls', 0)} 次呼叫，{month.get('total_tokens', 0):,} token\n"
        f"　估計費用 ${month.get('cost', 0.0):.4f}\n\n"
        "目前沒有設上限，儘管用。"
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    await update.effective_message.reply_text(
        f"你的 user id：{user.id}\n這個對話的 chat id：{chat.id}"
    )


# ── 管理員 ──────────────────────────────────────────────


@admin_only
async def cmd_issue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    label = " ".join(context.args).strip() if context.args else None
    ttl = svc.cfg.invite_ttl_seconds
    code = await svc.access.issue_invite(label, update.effective_user.id, ttl_seconds=ttl)

    minutes = ttl // 60
    window = f"{minutes} 分鐘" if ttl >= 60 else f"{ttl} 秒"

    await update.effective_message.reply_text(
        f"邀請碼：`{code}`\n\n"
        f"連結：https://t.me/{svc.bot_username}?start={code}\n\n"
        f"備註：{label or '（無）'}\n"
        f"**{window}內有效，只能用一次。**\n"
        "現在就傳給對方，過期就要重新生成。",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    code = context.args[0] if context.args else ""
    if not code:
        await update.effective_message.reply_text("用法：/revoke <邀請碼>")
        return
    ok = await svc.access.revoke_invite(code)
    await update.effective_message.reply_text("已撤銷。" if ok else "找不到這張邀請碼。")


@admin_only
async def cmd_invites(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    rows = await svc.access.list_invites()
    if not rows:
        await update.effective_message.reply_text("還沒發出過邀請碼。")
        return

    lines = ["邀請碼清單（最新的在前）：\n"]
    for row in rows:
        if row["revoked"]:
            state = "已撤銷"
        elif row["bound_user_id"]:
            state = f"已綁定 {row['bound_user_id']}"
        else:
            state = "未使用"
        lines.append(f"· {row['label'] or '（無備註）'} — {state}（{row['created_at']}）")
    await update.effective_message.reply_text("\n".join(lines))


@admin_only
async def cmd_allowgroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    chat_id = int(context.args[0]) if context.args and context.args[0].lstrip("-").isdigit() else update.effective_chat.id
    ok = await svc.access.set_group_allowed(chat_id, True)
    await update.effective_message.reply_text(
        f"群組 {chat_id} 已授權。" if ok else f"沒有 {chat_id} 的紀錄，先把本鯨拉進那個群組。"
    )


@admin_only
async def cmd_denygroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    chat_id = int(context.args[0]) if context.args and context.args[0].lstrip("-").isdigit() else update.effective_chat.id
    ok = await svc.access.set_group_allowed(chat_id, False)
    await update.effective_message.reply_text(
        f"群組 {chat_id} 已停用。" if ok else f"沒有 {chat_id} 的紀錄。"
    )


@admin_only
async def cmd_groups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    rows = await svc.access.list_groups()
    if not rows:
        await update.effective_message.reply_text("還沒有任何群組紀錄。")
        return
    lines = ["群組清單：\n"]
    for row in rows:
        state = "已授權" if row["allowed"] else "未授權"
        lines.append(f"· {row['title'] or '（無名稱）'} — {state}\n  id: {row['chat_id']}")
    await update.effective_message.reply_text("\n".join(lines))


@admin_only
async def cmd_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    days = int(context.args[0]) if context.args and context.args[0].isdigit() else 7
    total = await svc.usage.overall(days)
    top = await svc.usage.top_users(days)

    lines = [
        f"近 {days} 天總帳：\n",
        f"　呼叫 {total.get('calls', 0):,} 次",
        f"　輸入 {total.get('prompt_tokens', 0):,}（快取命中 {total.get('cached_tokens', 0):,}）",
        f"　輸出 {total.get('completion_tokens', 0):,}",
        f"　圖片 {total.get('image_tokens', 0):,}",
        f"　費用 ${total.get('cost', 0.0):.4f}",
    ]
    if top:
        lines.append("\n用量排行：")
        for row in top:
            name = row["display_name"] or str(row["tg_user_id"])
            lines.append(f"　{name} — {row['tokens'] or 0:,} token")
    await update.effective_message.reply_text("\n".join(lines))


@admin_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    users = await svc.db.fetchval("SELECT COUNT(*) FROM users WHERE status = 'active'", default=0)
    pending = await svc.db.fetchval("SELECT COUNT(*) FROM users WHERE status = 'pending'", default=0)
    sessions = await svc.db.fetchval("SELECT COUNT(*) FROM sessions", default=0)
    cached = await svc.db.fetchval("SELECT COUNT(*) FROM group_cache", default=0)
    uptime = int(time.time() - svc.started_at) if svc.started_at else 0

    await update.effective_message.reply_text(
        "運轉狀態：\n"
        f"　已授權使用者　{users}\n"
        f"　待驗證　　　　{pending}\n"
        f"　對話 session　{sessions}\n"
        f"　群組快取　　　{cached} 則\n"
        f"　累計錯誤　　　{svc.errors}\n"
        f"　運行時間　　　{uptime // 3600} 小時 {uptime % 3600 // 60} 分"
    )


@admin_only
async def cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("用法：/block <user_id>")
        return
    await svc.access.set_status(int(context.args[0]), "blocked")
    await update.effective_message.reply_text("已封鎖。")


@admin_only
async def cmd_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("用法：/unblock <user_id>")
        return
    await svc.access.set_status(int(context.args[0]), "active")
    await update.effective_message.reply_text("已解除封鎖。")


@admin_only
async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    svc.persona.reload(svc.cfg.persona_file)
    await update.effective_message.reply_text("人設已重新載入。")
