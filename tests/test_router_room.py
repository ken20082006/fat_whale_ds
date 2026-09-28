"""群組「聊天室模式」嘅 handler 行為 —— 兩個開關嘅四種組合。

假物件，唔連 Telegram、唔連 Hermes（同 `test_router_handlers.py` 一樣）。
`RoomMode` 自己嘅讀寫喺 `test_room_mode.py`，窗口邏輯喺 `test_chain_window.py`；
呢度淨係測**接線**。

四種組合：

    開關 A（一個群一條對話）  開關 B（讀最近群組對話）
    ─────────────────────  ──────────────────────
    關                     關   ← 原本行為，一個字都唔可以變
    開                     關
    關                     開
    開                     開   ← 完整嘅「聊天室模式」
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from telegram.constants import ChatType

from dafeijing.router.handlers import on_group_message
from dafeijing.router.hermes import HermesError, Reply

CHAT = -1001324180809
BOT_ID = 999
USER_ID = 216587605
OTHER_USER = 121
CONV_A = f"grp:{CHAT}:100"
ROOM = f"room:{CHAT}"

WINDOW = [
    {
        "message_id": 800,
        "user_id": OTHER_USER,
        "display_name": "乙",
        "text": "尋日講開嗰隻船",
        "created_at": "2026-09-26 02:00:00",  # → 09-26 10:00 HKT
    },
    {
        "message_id": 801,
        "user_id": USER_ID,
        "display_name": "陳大文",
        "text": "改咗期未",
        "created_at": "2026-09-27 02:30:00",  # → 09-27 10:30 HKT
    },
]


# ── 假物件 ──────────────────────────────────────────────


class FakeChain:
    def __init__(self, conversations=None, chain=None, window=None):
        self._conversations = conversations or {}
        self._chain = chain or []
        self._window = window if window is not None else []
        self.window_calls: list[dict] = []
        self.replies: list[dict] = []
        self.reply_updates: list[object] = []

    async def conversation_of(self, chat_id, message_id):
        return self._conversations.get(message_id)

    async def resolve(self, chat_id, message_id):
        return list(self._chain)

    async def roster(self, chat_id):
        return {}

    async def cache_from_update(self, message) -> None:
        self.reply_updates.append(message)

    async def cache_message(self, **kwargs) -> None:
        self.replies.append(kwargs)

    async def window(self, chat_id, **kwargs):
        self.window_calls.append({"chat_id": chat_id, **kwargs})
        return list(self._window)


class FakeHermes:
    def __init__(self, text="本鯨收到", boom=False):
        self.calls: list[tuple[str, str]] = []
        self._text = text
        self._boom = boom

    async def ask(self, conversation, text, images=None) -> Reply:
        self.calls.append((conversation, text))
        if self._boom:
            raise HermesError("Hermes 回 500")
        return Reply(text=self._text, input_tokens=100, output_tokens=5)


class FakeBot:
    def __init__(self, message_id=9001):
        self.id = BOT_ID
        self.sent: list[tuple[int, str]] = []
        self._message_id = message_id

    async def send_message(self, chat_id, text, **_kw):
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=self._message_id)

    async def send_chat_action(self, *_a, **_k):
        return True

    async def send_sticker(self, chat_id, file_id, **_kw):
        return SimpleNamespace(message_id=9500)


def _message(*, text="咁你覺得點", message_id=950, reply_to=None):
    bot = FakeBot()

    async def reply_text(body, **_kw):
        bot.sent.append((CHAT, body))
        return SimpleNamespace(message_id=9001)

    message = SimpleNamespace(
        chat=SimpleNamespace(id=CHAT, type=ChatType.SUPERGROUP),
        chat_id=CHAT,
        message_id=message_id,
        from_user=SimpleNamespace(id=USER_ID, full_name="陳大文", username="ken"),
        text=text,
        caption=None,
        entities=None,
        caption_entities=None,
        via_bot=None,
        reply_to_message=reply_to,
        message_thread_id=None,
        get_bot=lambda: bot,
        bot=bot,
        sticker=None,
        animation=None,
        video=None,
        video_note=None,
        photo=None,
        document=None,
    )
    message.reply_text = reply_text
    return message


def _reply_to_bot(message_id=9001):
    return SimpleNamespace(
        message_id=message_id,
        from_user=SimpleNamespace(id=BOT_ID, full_name="大肥鯨"),
        text="本鯨講過嘅嘢",
        caption=None,
        sticker=None,
        animation=None,
        video=None,
        video_note=None,
        photo=None,
        document=None,
    )


async def _no_profile(_chat_id):
    return None


def _services(chain, hermes, *, room_on=False, read_on=False, cursor=None):
    async def ensure(*_a, **_k):
        return None

    async def notes(*_a, **_k):
        return []

    async def status(_chat_id):
        return room_on, read_on

    async def get_cursor(_conversation):
        return cursor

    async def set_cursor(_conversation, _message_id):
        chain.replies.append({"_cursor": (_conversation, _message_id)})
        return None

    return SimpleNamespace(
        cfg=SimpleNamespace(
            maintenance_mode=False,
            timezone_offset_hours=8,
            group_room_window_messages=30,
            group_room_window_tokens=6_000,
            admin_ids=(USER_ID,),
            model="test/model",
            hermes_input_price=0.30,
            hermes_output_price=1.20,
            allow_bots=False,
            self_name="本鯨",
        ),
        chain=chain,
        hermes=hermes,
        rooms=SimpleNamespace(
            status=status, cursor=get_cursor, set_cursor=set_cursor
        ),
        sessions=SimpleNamespace(notes=notes, get_group_profile=_no_profile),
        profiler=SimpleNamespace(schedule=lambda _cid: None),
        usage=SimpleNamespace(record=lambda **_kw: _done()),
        runaway=SimpleNamespace(allow=lambda _c: True),
        memory=SimpleNamespace(schedule=lambda **_kw: None),
        stickers=SimpleNamespace(menu=lambda: "", resolve=lambda _i: None),
        seen_conversations=set(),
        limiter=SimpleNamespace(check=lambda _uid: (True, 0)),
        access=SimpleNamespace(ensure=ensure, is_active=lambda _uid: True),
        is_admin=lambda _uid: True,
        bot_id=BOT_ID,
        bot_name="大肥鯨",
        errors=0,
    )


async def _done():
    return None


def _run_group(message, svc, bot=None):
    import dafeijing.router.handlers as h

    async def yes(*_a, **_k):
        return True

    h.group_usable, h.is_addressed_to_bot = yes, lambda *_a: True
    update = SimpleNamespace(
        effective_message=message, effective_user=message.from_user
    )
    asyncio.run(on_group_message(update, SimpleNamespace(
        bot_data={"services": svc}, bot=bot or FakeBot()
    )))


# ── 開關 A：對話名 ──────────────────────────────────────


def test_room_on_uses_one_conversation_for_the_whole_group():
    """⭐ 開關 A 嘅全部意義：引用邊個都係同一條對話。"""
    chain = FakeChain({9001: CONV_A})  # 引用本鯨，舊路徑會續返 CONV_A
    hermes = FakeHermes()
    _run_group(
        _message(reply_to=_reply_to_bot(9001)),
        _services(chain, hermes, room_on=True),
    )

    assert hermes.calls[0][0] == ROOM


def test_room_off_keeps_the_old_per_thread_conversation():
    chain = FakeChain({9001: CONV_A})
    hermes = FakeHermes()
    _run_group(
        _message(reply_to=_reply_to_bot(9001)),
        _services(chain, hermes, room_on=False),
    )

    assert hermes.calls[0][0] == CONV_A


def test_room_on_does_not_write_the_conversation_into_the_cache():
    """⚠️ 刻意寫 NULL。

    寫咗 `room:<chat>` 嘅話，第日 `/room off` 之後有人引用一則 room 年代
    嘅回覆，`conversation_of()` 會搵返條 room 大歷史 —— 靜靜哋將成個群
    拉入去。寫 NULL 就乾淨。
    """
    chain = FakeChain()
    _run_group(_message(), _services(chain, FakeHermes(), room_on=True))

    cached = [r for r in chain.replies if "conversation" in r]
    assert cached and cached[0]["conversation"] is None


def test_room_off_still_writes_the_conversation():
    """唔可以連舊路徑都改成 NULL —— 引用串機制靠佢。"""
    chain = FakeChain()
    _run_group(_message(), _services(chain, FakeHermes(), room_on=False))

    cached = [r for r in chain.replies if "conversation" in r]
    assert cached and cached[0]["conversation"] == f"grp:{CHAT}:950"


# ── 開關 B：近況窗口 ────────────────────────────────────


def test_read_on_attaches_the_window_before_the_trigger():
    chain = FakeChain(window=WINDOW)
    hermes = FakeHermes()
    _run_group(_message(), _services(chain, hermes, read_on=True))

    body = hermes.calls[0][1]
    assert "[群組近況開始]" in body and "[群組近況結束]" in body
    assert "[乙|121] 09-26 10:00" in body
    assert "[陳大文|216587605] 09-27 10:30" in body
    assert body.index("[群組近況結束]") < body.index("咁你覺得點"), (
        "近況要排喺觸發嗰句之前"
    )


def test_window_excludes_the_bot_and_stops_at_the_trigger():
    chain = FakeChain(window=WINDOW)
    _run_group(_message(message_id=950), _services(chain, FakeHermes(), read_on=True))

    call = chain.window_calls[0]
    assert call["exclude_user_id"] == BOT_ID, "本鯨自己嘅訊息要隔走"
    assert call["before_message_id"] == 950, "唔可以包觸發嗰則自己"
    assert call["limit"] == 30


def test_window_uses_the_cursor_as_its_starting_point():
    chain = FakeChain(window=WINDOW)
    _run_group(_message(), _services(chain, FakeHermes(), read_on=True, cursor=700))

    assert chain.window_calls[0]["after_message_id"] == 700


def test_read_off_sends_no_window_at_all():
    """⭐ 開關 B 關 = 原本行為，body 一個字都唔可以變。"""
    chain = FakeChain(window=WINDOW)
    hermes = FakeHermes()
    _run_group(_message(), _services(chain, hermes, read_on=False))

    assert chain.window_calls == []
    body = hermes.calls[0][1]
    assert "[群組近況" not in body
    assert body.endswith(f"[陳大文|{USER_ID}]\n咁你覺得點")


# ── 去重 ────────────────────────────────────────────────


def test_window_and_quote_chain_do_not_repeat_the_same_message():
    """引用嗰則多數啱啱先講過 —— 會同時喺窗口同串度出現。

    冇去重嘅話同一句會出兩次，模型會以為對方講咗兩次。
    """
    shared = {
        "message_id": 802,
        "user_id": OTHER_USER,
        "display_name": "乙",
        "text": "獨一無二嘅句子",
        "created_at": "2026-09-27 03:00:00",
    }
    chain = FakeChain(chain=[shared], window=[shared])
    hermes = FakeHermes()
    _run_group(
        _message(reply_to=_reply_stub_for_chain(802)),
        _services(chain, hermes, read_on=True),
    )

    assert hermes.calls[0][1].count("獨一無二嘅句子") == 1


def test_quote_chain_entries_not_in_the_window_still_appear():
    """窗口冇嘅串訊息要照出 —— 唔可以為咗去重而漏咗。"""
    older = {
        "message_id": 500,
        "user_id": OTHER_USER,
        "display_name": "丙",
        "text": "好耐之前嗰句",
        "created_at": "2026-09-20 02:00:00",
    }
    chain = FakeChain(chain=[older], window=WINDOW)
    hermes = FakeHermes()
    _run_group(
        _message(reply_to=_reply_stub_for_chain(500)),
        _services(chain, hermes, read_on=True),
    )

    assert "好耐之前嗰句" in hermes.calls[0][1]


def _reply_stub_for_chain(message_id: int):
    """被引用嘅係普通人（唔係本鯨）—— 咁 `_quoted_entries` 先會追串。"""
    return SimpleNamespace(
        message_id=message_id,
        from_user=SimpleNamespace(id=OTHER_USER, full_name="乙"),
        text="被引用嗰句",
        caption=None,
        sticker=None,
        animation=None,
        video=None,
        video_note=None,
        photo=None,
        document=None,
    )


# ── cursor ──────────────────────────────────────────────


def _cursor_calls(chain):
    return [r["_cursor"] for r in chain.replies if "_cursor" in r]


def test_successful_reply_advances_the_cursor():
    chain = FakeChain()
    _run_group(
        _message(message_id=950),
        _services(chain, FakeHermes(), room_on=True, read_on=True),
    )

    assert _cursor_calls(chain) == [(ROOM, 950)]


def test_cursor_is_keyed_by_the_actual_conversation():
    """開關 A 關咗嗰陣 cursor 要跟**嗰條引用串**，唔係跟個群 ——
    否則兩條串會互相搶位標，一條睇過嘅另一條就收唔到。"""
    chain = FakeChain()
    _run_group(
        _message(message_id=950),
        _services(chain, FakeHermes(), room_on=False, read_on=True),
    )

    assert _cursor_calls(chain) == [(f"grp:{CHAT}:950", 950)]


def test_hermes_failure_does_not_advance_the_cursor():
    """⭐ 失敗就唔可以推 —— 嗰啲訊息 Hermes 根本冇收過，下次要重送。"""
    chain = FakeChain()
    _run_group(
        _message(message_id=950),
        _services(chain, FakeHermes(boom=True), read_on=True),
    )

    assert _cursor_calls(chain) == []


def test_read_off_does_not_advance_the_cursor():
    """冇送過近況就唔應該推 —— 否則日後開返 B 會漏咗中間嗰批。"""
    chain = FakeChain()
    _run_group(_message(message_id=950), _services(chain, FakeHermes(), read_on=False))

    assert _cursor_calls(chain) == []
