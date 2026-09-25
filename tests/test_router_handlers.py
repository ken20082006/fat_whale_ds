"""Router handlers —— 對話路由同補快取。

用假物件，唔連 Telegram、唔連 Hermes。真嘅端到端要人手發訊息先試到。

**最關鍵嘅兩條**：
1. 冇引用 → 新對話；有引用 → 續返同一條
2. bot 自己嘅回覆一定要補快取，否則別人回覆本鯨時條串會斷
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


class FakeChain:
    def __init__(self, root: int, chain: list[dict] | None = None):
        self.root = root
        self.chain = chain or []
        self.replies: list[dict] = []
        self.reply_updates: list[object] = []

    async def root_id(self, chat_id: int, message_id: int) -> int:
        return self.root

    async def resolve(self, chat_id: int, message_id: int) -> list[dict]:
        return self.chain

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
    """夠 `reply_markdown` 同 `typing` 用嘅假 bot。"""

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
    )
    message.reply_text = reply_text
    return message


def _context(svc, bot: FakeBot | None = None):
    return SimpleNamespace(
        bot_data={"services": svc},
        bot=bot or FakeBot(),
    )


def _services(chain, hermes, *, maintenance: bool = False):
    async def ensure(*_a, **_k):
        return None

    return SimpleNamespace(
        cfg=SimpleNamespace(
            maintenance_mode=maintenance,
            admin_ids=(USER_ID,),
            rate_per_minute=20,
        ),
        chain=chain,
        hermes=hermes,
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
        h.group_usable = lambda *a, **k: _true()
        h.is_addressed_to_bot = lambda *a, **k: True
        try:
            await on_group_message(update, _context(svc))
        finally:
            h.group_usable, h.is_addressed_to_bot = original_usable, original_addressed

    asyncio.run(scenario())


async def _true() -> bool:
    return True


# ── 冇引用 → 開新對話 ──────────────────────────────────


def test_bare_mention_starts_a_brand_new_conversation():
    """**呢個就係用戶要嘅嘢**：唔引用任何嘢，就應該開新對話，
    唔係續返上一條。"""
    chain = FakeChain(root=500)  # 冇引用 → 串根就係自己
    hermes = FakeHermes()
    svc = _services(chain, hermes)

    _run_group(_message(message_id=500), svc)

    assert hermes.calls[0][0] == f"grp:{CHAT}:500"


def test_two_bare_mentions_go_to_two_different_conversations():
    """連續兩次冇引用嘅 @ —— 兩個唔同嘅對話，唔可以續埋一齊。"""
    hermes = FakeHermes()

    _run_group(_message(message_id=500), _services(FakeChain(root=500), hermes))
    _run_group(_message(message_id=700), _services(FakeChain(root=700), hermes))

    assert hermes.calls[0][0] == f"grp:{CHAT}:500"
    assert hermes.calls[1][0] == f"grp:{CHAT}:700"
    assert hermes.calls[0][0] != hermes.calls[1][0]


# ── 有引用 → 續返同一條 ────────────────────────────────


def test_reply_into_a_chain_reuses_that_conversation():
    """引用咗一條串 → 用串根做對話名，即係續返嗰條。"""
    chain = FakeChain(root=100)  # 100 ← 200 ← 500
    hermes = FakeHermes()
    svc = _services(chain, hermes)

    reply_to = SimpleNamespace(from_user=SimpleNamespace(id=121), message_id=200)
    _run_group(_message(message_id=500, reply_to=reply_to), svc)

    assert hermes.calls[0][0] == f"grp:{CHAT}:100"


def test_reply_to_the_bot_reuses_the_same_conversation():
    """回覆本鯨 —— 條串嘅根冇變，所以係同一條對話。"""
    chain = FakeChain(root=100)
    hermes = FakeHermes()
    svc = _services(chain, hermes)

    reply_to = SimpleNamespace(from_user=SimpleNamespace(id=BOT_ID), message_id=9001)
    _run_group(_message(message_id=950, reply_to=reply_to), svc)

    assert hermes.calls[0][0] == f"grp:{CHAT}:100"


# ── 補快取自己嘅回覆 ────────────────────────────────────


def test_bot_reply_is_cached_so_the_chain_never_breaks():
    """**最易漏嘅一步。** Telegram 唔會將 bot 自己嘅訊息回傳畀 bot。
    唔補嘅話，別人回覆本鯨就追唔到串根，嗰句會變成一條新串。"""
    chain = FakeChain(root=500)
    svc = _services(chain, FakeHermes(text="本鯨答你"))

    _run_group(_message(message_id=500), svc)

    assert len(chain.replies) == 1
    cached = chain.replies[0]
    assert cached["message_id"] == 9001
    assert cached["reply_to_id"] == 500, "要指返觸發嗰則，唔係指串根"
    assert cached["user_id"] == BOT_ID, "用 bot_id 標記，pending_since_bot 靠佢認"
    assert cached["text"] == "本鯨答你"


def test_reply_to_message_is_cached_for_chain_walking():
    """Bot API 收唔到其他 bot 嘅訊息，所以要靠即時更新補上。"""
    chain = FakeChain(root=100)
    svc = _services(chain, FakeHermes())

    reply_to = SimpleNamespace(from_user=SimpleNamespace(id=121), message_id=200)
    _run_group(_message(message_id=500, reply_to=reply_to), svc)

    assert chain.reply_updates == [reply_to]


def test_nothing_is_cached_when_hermes_fails():
    """Hermes 爆咗就冇回覆，唔應該補一筆空快取落去。"""
    chain = FakeChain(root=500)
    svc = _services(chain, FakeHermes(boom=True))

    _run_group(_message(message_id=500), svc)

    assert chain.replies == []


# ── 送咩入 Hermes ──────────────────────────────────────


def test_bare_mention_sends_only_the_trigger():
    """冇歷史 → 只送觸發嗰句（串根就係自己，前面冇嘢）。"""
    chain = FakeChain(root=500, chain=[])
    hermes = FakeHermes()
    svc = _services(chain, hermes)

    _run_group(_message(text="今日隻船係咪要改期？", message_id=500), svc)

    _, body = hermes.calls[0]
    assert body == f"[陳大文|{USER_ID}]\n今日隻船係咪要改期？"


def test_bystander_messages_are_included():
    """bot 回覆咗 → 乙插嘴（冇 @）→ 甲 @ 本鯨。乙嗰句都要送。"""
    chain = FakeChain(
        root=100,
        chain=[
            {"message_id": 100, "user_id": 111, "display_name": "甲", "text": "問"},
            {"message_id": 200, "user_id": BOT_ID, "display_name": "大肥鯨", "text": "答"},
            {"message_id": 300, "user_id": 222, "display_name": "乙", "text": "我插嘴"},
            {"message_id": 500, "user_id": USER_ID, "display_name": "陳大文", "text": "點睇"},
        ],
    )
    hermes = FakeHermes()
    svc = _services(chain, hermes)

    _run_group(_message(text="點睇", message_id=500), svc)

    _, body = hermes.calls[0]
    assert "乙" in body and "我插嘴" in body
    assert "問" not in body, "bot 已回覆過嘅部分唔應該重送"
    assert body.endswith(f"[陳大文|{USER_ID}]\n點睇")


# ── 私聊 ────────────────────────────────────────────────


def test_private_message_uses_one_conversation():
    hermes = FakeHermes()
    svc = _services(FakeChain(root=0), hermes)
    message = _message(chat_type=ChatType.PRIVATE, message_id=1)
    update = SimpleNamespace(effective_message=message, effective_user=message.from_user)

    asyncio.run(on_private_message(update, _context(svc)))
    asyncio.run(on_private_message(update, _context(svc)))

    assert hermes.calls[0][0] == hermes.calls[1][0] == f"dm:{CHAT}"


def test_private_topics_are_separate_conversations():
    hermes = FakeHermes()
    svc = _services(FakeChain(root=0), hermes)
    update = SimpleNamespace(effective_message=None, effective_user=None)

    for thread_id in (1976030, 1976034):
        message = _message(chat_type=ChatType.PRIVATE, thread_id=thread_id)
        update = SimpleNamespace(
            effective_message=message, effective_user=message.from_user
        )
        asyncio.run(on_private_message(update, _context(svc)))

    assert hermes.calls[0][0] != hermes.calls[1][0]
    assert all(c[0].startswith("dm:") for c in hermes.calls)
