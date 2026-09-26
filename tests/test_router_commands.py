"""Router 嘅指令。

大部分係直接借大肥鯨嗰啲，所以呢度主要測**Router 自己改寫嘅三個**
（`/help`、`/context`、`/new`）同註冊本身 —— 借返嚟嗰批由大肥鯨自己嘅
測試守住。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from telegram.constants import ChatType
from telegram.ext import Application, CommandHandler

from dafeijing.router.commands import cmd_context, cmd_help, cmd_new, register
from dafeijing.router.conversation import dm_conversation

CHAT = -1001324180809
USER_ID = 216587605


def _fake_cfg(self_name: str = "本鯨") -> SimpleNamespace:
    """`RouterServices.cfg` 嘅最小替身。

    真嘅 `RouterServices` 一定有 `cfg`（見 services.py），指令靠佢攞
    `self_name` —— Router 係兩隻 bot 共用嘅代碼，角色名由設定帶入。
    預設值同大肥鯨一樣，所以下面啲斷言照舊成立。
    """
    return SimpleNamespace(self_name=self_name)


# ── 註冊 ────────────────────────────────────────────────


def _registered_commands() -> set[str]:
    app = Application.builder().token("123:abc").build()
    register(app)
    names: set[str] = set()
    for handlers in app.handlers.values():
        for handler in handlers:
            if isinstance(handler, CommandHandler):
                names.update(handler.commands)
    return names


def test_expected_commands_are_registered():
    assert _registered_commands() == {
        "start",
        "help",
        "new",
        "context",
        "remember",
        "forget",
        "quota",
        "id",
        "issue",
        "revoke",
        "invites",
        "allowgroup",
        "denygroup",
        "groups",
        "block",
        "unblock",
        "cost",
        "stats",
    }


def test_commands_that_would_lie_are_not_registered():
    """呢啲控制大肥鯨自己個腦 —— 而家個腦係 Hermes，照註冊會誤導。

    `/tune`、`/reload_persona` 改嘅係大肥鯨嘅執行期設定同人設檔，
    Router 全部唔理；`/undo`、`/export` 要讀 session 歷史，嗰啲喺 Hermes。
    """
    names = _registered_commands()
    for dead in ("tune", "reload_persona", "undo", "export", "vibe", "think", "search"):
        assert dead not in names, f"/{dead} 唔應該註冊"


# ── /help ───────────────────────────────────────────────


def _help_text(is_admin: bool, self_name: str = "本鯨") -> str:
    sent: list[str] = []

    async def reply_text(text, **_kw):
        sent.append(text)

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=USER_ID),
        effective_message=SimpleNamespace(reply_text=reply_text),
    )
    svc = SimpleNamespace(is_admin=lambda _uid: is_admin, cfg=_fake_cfg(self_name))
    context = SimpleNamespace(bot_data={"services": svc})

    asyncio.run(cmd_help(update, context))
    return sent[0]


def test_help_lists_the_real_commands():
    text = _help_text(is_admin=False)
    for name in ("/new", "/context", "/remember", "/forget", "/id", "/help"):
        assert name in text


def test_help_hides_admin_section_from_normal_users():
    assert "/issue" not in _help_text(is_admin=False)


def test_help_shows_admin_section_to_admins():
    assert "/issue" in _help_text(is_admin=True)


def test_help_uses_the_configured_self_name():
    """`self_name` 令同一份 Router 代碼服務唔同角色（見 settings.self_name）。

    大肥鯨唔填就係「本鯨」；貝爾法斯特填「貝爾法斯特」。
    """
    assert "本鯨識呢幾樣" in _help_text(is_admin=False)
    text = _help_text(is_admin=False, self_name="貝爾法斯特")
    assert "貝爾法斯特識呢幾樣" in text
    assert "本鯨" not in text


def test_help_does_not_mention_commands_that_do_not_exist():
    """大肥鯨嗰版本會講 /tune /vibe /think —— Router 冇，講咗就係講大話。"""
    text = _help_text(is_admin=True)
    for dead in ("/tune", "/vibe", "/think", "/undo"):
        assert dead not in text


# ── /context ────────────────────────────────────────────


def _context_text(counts):
    sent: list[str] = []

    async def reply_text(text, **_kw):
        sent.append(text)

    async def note_counts(_uid):
        return counts

    async def is_active(_uid):
        return True

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=USER_ID),
        effective_message=SimpleNamespace(reply_text=reply_text),
    )
    svc = SimpleNamespace(
        sessions=SimpleNamespace(note_counts=note_counts),
        access=SimpleNamespace(is_active=is_active),
        is_admin=lambda _uid: False,
        bot_username="deepseek_girl_bot",
        cfg=_fake_cfg(),
    )
    context = SimpleNamespace(bot_data={"services": svc})

    asyncio.run(cmd_context(update, context))
    return sent[0]


def test_context_lists_note_counts():
    text = _context_text([("private", 3), (f"group:{CHAT}", 5)])
    assert "3 則" in text
    assert "5 則" in text


def test_context_says_so_when_there_are_no_notes():
    assert "冇你嘅筆記" in _context_text([])


def test_context_does_not_pretend_to_know_the_conversation():
    """對話歷史唔喺呢邊 —— 每條引用串係一條獨立對話，由 Hermes 管。"""
    assert "Hermes" in _context_text([])


# ── /new ────────────────────────────────────────────────


def _run_new(chat_type):
    sent: list[str] = []

    async def reply_text(text, **_kw):
        sent.append(text)

    async def is_active(_uid):
        return True

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=USER_ID),
        effective_chat=SimpleNamespace(id=CHAT, type=chat_type),
        effective_message=SimpleNamespace(reply_text=reply_text),
    )
    svc = SimpleNamespace(
        access=SimpleNamespace(is_active=is_active),
        is_admin=lambda _uid: True,
        bot_username="deepseek_girl_bot",
        dm_generation=0,
        cfg=_fake_cfg(),
    )
    context = SimpleNamespace(bot_data={"services": svc})

    asyncio.run(cmd_new(update, context))
    return sent[0], svc


def test_new_in_a_group_explains_it_is_not_needed():
    """群組嗰邊每條引用串本來就係一條獨立對話 —— 唔使 /new。"""
    text, svc = _run_new(ChatType.SUPERGROUP)
    assert "唔引用" in text
    assert svc.dm_generation == 0, "群組唔應該遞增世代"


def test_new_in_private_bumps_the_generation():
    text, svc = _run_new(ChatType.PRIVATE)
    assert svc.dm_generation == 1
    assert "新" in text


# ── /new 對對話名嘅影響 ────────────────────────────────


def test_generation_changes_the_conversation_name():
    assert dm_conversation(CHAT) == dm_conversation(CHAT, None, 0), "世代 0 唔應該改個名"
    assert dm_conversation(CHAT, None, 1) != dm_conversation(CHAT, None, 0)


def test_each_generation_is_its_own_conversation():
    names = {dm_conversation(CHAT, None, g) for g in range(5)}
    assert len(names) == 5


def test_generation_keeps_the_topic_split():
    """有 topic 就照樣分開，世代唔應該蓋過佢。"""
    assert dm_conversation(CHAT, 100, 1) != dm_conversation(CHAT, 200, 1)