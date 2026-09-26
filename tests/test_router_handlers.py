"""Router handlers —— 對話路由同補快取。

用假物件，唔連 Telegram、唔連 Hermes。真嘅端到端要人手發訊息先試到。

**規則（用戶定死）：只有引用本鯨嘅回答先算同一串。**

    用戶1 @bot       → 開新對話
    bot 回答 R1
    用戶2 引用 R1    → 同一條
    用戶3 引用 用戶2  → **唔算**，開新一條
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from telegram.constants import ChatType

from dafeijing.router.handlers import on_group_message, on_private_message
from dafeijing.router.hermes import HermesError, Reply

CHAT = -1001324180809
BOT_ID = 999
USER_ID = 216587605
OTHER_USER = 121
CONV_A = f"grp:{CHAT}:100"


class FakeChain:
    """`conversations` 模擬 group_cache 入面本鯨嗰幾則回覆嘅對話名。"""

    def __init__(self, conversations: dict[int, str] | None = None):
        self._conversations = conversations or {}
        self.replies: list[dict] = []
        self.reply_updates: list[object] = []

    async def conversation_of(self, chat_id: int, message_id: int) -> str | None:
        return self._conversations.get(message_id)

    async def cache_from_update(self, message) -> None:
        self.reply_updates.append(message)

    async def cache_message(self, **kwargs) -> None:
        self.replies.append(kwargs)


class FakeHermes:
    def __init__(self, text: str = "本鯨收到", boom: bool = False):
        self.calls: list[tuple[str, str]] = []
        self._text = text
        self._boom = boom

    async def ask(self, conversation: str, text: str) -> Reply:
        self.calls.append((conversation, text))
        if self._boom:
            raise HermesError("Hermes 回 500")
        return Reply(text=self._text, input_tokens=100, output_tokens=5)


class FakeBot:
    def __init__(self, message_id: int = 9001):
        self.id = BOT_ID
        self.sent: list[tuple[int, str]] = []
        self._message_id = message_id

    async def send_message(self, chat_id, text, **_kw):
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=self._message_id)

    async def send_chat_action(self, *_a, **_k):
        return True


def _message(
    *,
    text: str = "本鯨喺唔喺度",
    message_id: int = 500,
    reply_to=None,
    chat_type=ChatType.SUPERGROUP,
    thread_id=None,
    bot: FakeBot | None = None,
):
    bot = bot or FakeBot()

    async def reply_text(body, **_kw):
        bot.sent.append((CHAT, body))
        return SimpleNamespace(message_id=9001)

    message = SimpleNamespace(
        chat=SimpleNamespace(id=CHAT, type=chat_type),
        chat_id=CHAT,
        message_id=message_id,
        from_user=SimpleNamespace(id=USER_ID, full_name="陳大文", username="ken"),
        text=text,
        caption=None,
        entities=None,
        caption_entities=None,
        via_bot=None,
        reply_to_message=reply_to,
        message_thread_id=thread_id,
        get_bot=lambda: bot,
        bot=bot,
        # pick_file() 直接讀呢幾個屬性，缺一個就 AttributeError
        sticker=None,
        animation=None,
        video=None,
        video_note=None,
        photo=None,
        document=None,
    )
    message.reply_text = reply_text
    return message


def _reply_to_bot(message_id: int = 9001):
    return SimpleNamespace(
        message_id=message_id, from_user=SimpleNamespace(id=BOT_ID, full_name="大肥鯨")
    )


def _reply_to_user(message_id: int = 300, user_id: int = OTHER_USER):
    return SimpleNamespace(
        message_id=message_id, from_user=SimpleNamespace(id=user_id, full_name="用戶2")
    )


def _context(svc, bot: FakeBot | None = None):
    return SimpleNamespace(bot_data={"services": svc}, bot=bot or FakeBot())


def _services(chain, hermes):
    async def ensure(*_a, **_k):
        return None

    async def notes(*_a, **_k):
        return []

    return SimpleNamespace(
        cfg=SimpleNamespace(maintenance_mode=False, admin_ids=(USER_ID,)),
        chain=chain,
        hermes=hermes,
        sessions=SimpleNamespace(notes=notes),
        memory=SimpleNamespace(schedule=lambda **_kw: None),
        limiter=SimpleNamespace(check=lambda _uid: (True, 0)),
        access=SimpleNamespace(ensure=ensure),
        is_admin=lambda _uid: True,
        bot_id=BOT_ID,
        bot_name="大肥鯨",
        bot_username="fatwhale_bot",
        errors=0,
    )


def _run_group(message, svc):
    update = SimpleNamespace(effective_message=message, effective_user=message.from_user)

    async def scenario():
        # patch 走「被指名」同「個群用得」兩個閘，集中測路由
        import dafeijing.router.handlers as h

        original_usable, original_addressed = h.group_usable, h.is_addressed_to_bot

        async def _true(*_a, **_k):
            return True

        h.group_usable, h.is_addressed_to_bot = _true, lambda *a, **k: True
        try:
            await on_group_message(update, _context(svc))
        finally:
            h.group_usable, h.is_addressed_to_bot = original_usable, original_addressed

    asyncio.run(scenario())


# ── 只有引用本鯨先算同一串 ──────────────────────────────


def test_bare_mention_starts_a_new_conversation():
    """唔引用任何嘢 → 開新對話，唔係續返上一條。"""
    hermes = FakeHermes()
    _run_group(_message(message_id=500), _services(FakeChain(), hermes))

    assert hermes.calls[0][0] == f"grp:{CHAT}:500"


def test_two_bare_mentions_are_two_conversations():
    hermes = FakeHermes()
    _run_group(_message(message_id=500), _services(FakeChain(), hermes))
    _run_group(_message(message_id=700), _services(FakeChain(), hermes))

    assert hermes.calls[0][0] != hermes.calls[1][0]


def test_quoting_the_bot_reuses_its_conversation():
    """**核心規則。** 用戶2 引用本鯨嘅回答 → 同一條串。"""
    chain = FakeChain({9001: CONV_A})
    hermes = FakeHermes()
    _run_group(
        _message(message_id=950, reply_to=_reply_to_bot(9001)),
        _services(chain, hermes),
    )

    assert hermes.calls[0][0] == CONV_A


def test_quoting_another_user_is_NOT_the_same_conversation():
    """**用戶特別強調嘅一條。** 用戶3 引用用戶2 —— 唔算同一串。"""
    chain = FakeChain({9001: CONV_A})  # 本鯨嗰則喺度，但引用嘅唔係佢
    hermes = FakeHermes()
    _run_group(
        _message(message_id=960, reply_to=_reply_to_user(300)),
        _services(chain, hermes),
    )

    assert hermes.calls[0][0] != CONV_A
    assert hermes.calls[0][0] == f"grp:{CHAT}:960", "應該用自己嘅 message_id 開新串"


def test_quoting_a_bot_message_with_no_record_starts_a_new_conversation():
    """引用本鯨，但快取冇紀錄（例如重啟後第一次見到）→ 唯有開新串。"""
    hermes = FakeHermes()
    _run_group(
        _message(message_id=970, reply_to=_reply_to_bot(1111)),
        _services(FakeChain(), hermes),
    )

    assert hermes.calls[0][0] == f"grp:{CHAT}:970"


def test_two_replies_to_the_same_bot_message_share_one_conversation():
    """兩個人都引用同一則本鯨回覆 → 同一條對話（多人共時參與）。"""
    chain = FakeChain({9001: CONV_A})
    hermes = FakeHermes()

    _run_group(_message(message_id=950, reply_to=_reply_to_bot(9001)), _services(chain, hermes))
    _run_group(_message(message_id=951, reply_to=_reply_to_bot(9001)), _services(chain, hermes))

    assert hermes.calls[0][0] == hermes.calls[1][0] == CONV_A


# ── 補快取自己嘅回覆 ────────────────────────────────────


def test_bot_reply_is_cached_with_its_conversation():
    """**最易漏嘅一步。** 冇記住對話名，別人引用本鯨就接唔返。"""
    chain = FakeChain()
    _run_group(_message(message_id=500), _services(chain, FakeHermes(text="本鯨答你")))

    assert len(chain.replies) == 1
    cached = chain.replies[0]
    assert cached["message_id"] == 9001
    assert cached["reply_to_id"] == 500, "要指返觸發嗰則"
    assert cached["user_id"] == BOT_ID
    assert cached["text"] == "本鯨答你"
    assert cached["conversation"] == f"grp:{CHAT}:500", "記住自己屬於邊條對話"


def test_cached_conversation_matches_what_was_asked():
    """引用本鯨嗰時，之後記落嘅對話名要同今次用嘅一樣。"""
    chain = FakeChain({9001: CONV_A})
    _run_group(
        _message(message_id=950, reply_to=_reply_to_bot(9001)),
        _services(chain, FakeHermes()),
    )

    assert chain.replies[0]["conversation"] == CONV_A


def test_reply_to_message_is_cached_for_lookup():
    """Bot API 收唔到其他 bot 嘅訊息，要靠即時更新補上。"""
    chain = FakeChain()
    parent = _reply_to_bot(9001)
    _run_group(_message(message_id=950, reply_to=parent), _services(chain, FakeHermes()))

    assert chain.reply_updates == [parent]


def test_nothing_is_cached_when_hermes_fails():
    chain = FakeChain()
    _run_group(_message(message_id=500), _services(chain, FakeHermes(boom=True)))

    assert chain.replies == []


# ── 送咩入 Hermes ──────────────────────────────────────


def test_only_the_triggering_message_is_sent():
    """一條對話入面除咗觸發訊息就係本鯨自己嘅回覆，Hermes 已經有齊歷史。"""
    chain = FakeChain({9001: CONV_A})
    hermes = FakeHermes()
    _run_group(
        _message(text="咁你覺得點", message_id=950, reply_to=_reply_to_bot(9001)),
        _services(chain, hermes),
    )

    _, body = hermes.calls[0]
    assert body.endswith(f"[陳大文|{USER_ID}]\n咁你覺得點")
    assert body.count("[陳大文|") == 1, "只送觸發嗰一句，唔可以重送歷史"


def test_media_only_message_still_has_text():
    hermes = FakeHermes()
    _run_group(
        _message(text="〔圖片〕", message_id=500),
        _services(FakeChain(), hermes),
    )

    assert hermes.calls[0][1].endswith(f"[陳大文|{USER_ID}]\n〔圖片〕")


# ── 私聊 ────────────────────────────────────────────────


def test_private_message_uses_one_conversation():
    hermes = FakeHermes()
    svc = _services(FakeChain(), hermes)
    message = _message(chat_type=ChatType.PRIVATE, message_id=1)
    update = SimpleNamespace(effective_message=message, effective_user=message.from_user)

    asyncio.run(on_private_message(update, _context(svc)))
    asyncio.run(on_private_message(update, _context(svc)))

    assert hermes.calls[0][0] == hermes.calls[1][0] == f"dm:{CHAT}"


def test_private_topics_are_separate_conversations():
    hermes = FakeHermes()
    svc = _services(FakeChain(), hermes)

    for thread_id in (1976030, 1976034):
        message = _message(chat_type=ChatType.PRIVATE, thread_id=thread_id)
        update = SimpleNamespace(
            effective_message=message, effective_user=message.from_user
        )
        asyncio.run(on_private_message(update, _context(svc)))

    assert hermes.calls[0][0] != hermes.calls[1][0]
    assert all(c[0].startswith("dm:") for c in hermes.calls)