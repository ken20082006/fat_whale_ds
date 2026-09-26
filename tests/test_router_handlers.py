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


async def _noop_record(**_kw) -> None:
    return None


class FakeChain:
    """`conversations` 模擬 group_cache 入面本鯨嗰幾則回覆嘅對話名。"""

    def __init__(
        self,
        conversations: dict[int, str] | None = None,
        chain: list[dict] | None = None,
    ):
        self._conversations = conversations or {}
        self._chain = chain or []
        self.replies: list[dict] = []
        self.reply_updates: list[object] = []

    async def conversation_of(self, chat_id: int, message_id: int) -> str | None:
        return self._conversations.get(message_id)

    async def resolve(self, chat_id: int, message_id: int) -> list[dict]:
        """引用串，由舊到新（同 ReplyChain.resolve 一樣）。"""
        return list(self._chain)

    async def roster(self, chat_id: int) -> dict[str, int]:
        return {}

    async def cache_from_update(self, message) -> None:
        self.reply_updates.append(message)

    async def cache_message(self, **kwargs) -> None:
        self.replies.append(kwargs)


class FakeHermes:
    def __init__(self, text: str = "本鯨收到", boom: bool = False):
        self.calls: list[tuple[str, str]] = []
        self.images: list[list[str] | None] = []
        self._text = text
        self._boom = boom

    async def ask(self, conversation: str, text: str, images=None) -> Reply:
        self.calls.append((conversation, text))
        self.images.append(images)
        if self._boom:
            raise HermesError("Hermes 回 500")
        return Reply(text=self._text, input_tokens=100, output_tokens=5)


class FakeBot:
    def __init__(self, message_id: int = 9001):
        self.id = BOT_ID
        self.sent: list[tuple[int, str]] = []
        self.stickers: list[tuple[int, str]] = []
        self._message_id = message_id

    async def send_message(self, chat_id, text, **_kw):
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=self._message_id)

    async def send_chat_action(self, *_a, **_k):
        return True

    async def send_sticker(self, chat_id, file_id, **_kw):
        self.stickers.append((chat_id, file_id))
        return SimpleNamespace(message_id=self._message_id + 1)


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


def _reply_stub(message_id: int, user_id: int, name: str, text=None):
    """被引用嗰則。要連媒體屬性 —— pick_file() 直接讀佢哋。"""
    return SimpleNamespace(
        message_id=message_id,
        from_user=SimpleNamespace(id=user_id, full_name=name),
        text=text,
        caption=None,
        sticker=None,
        animation=None,
        video=None,
        video_note=None,
        photo=None,
        document=None,
    )


def _reply_to_bot(message_id: int = 9001):
    return _reply_stub(message_id, BOT_ID, "大肥鯨")


def _reply_to_user(message_id: int = 300, user_id: int = OTHER_USER, text=None):
    return _reply_stub(message_id, user_id, "用戶2", text=text)


def _context(svc, bot: FakeBot | None = None):
    return SimpleNamespace(bot_data={"services": svc}, bot=bot or FakeBot())


async def _no_profile(_chat_id: int):
    return None


def _services(chain, hermes):
    async def ensure(*_a, **_k):
        return None

    async def notes(*_a, **_k):
        return []

    return SimpleNamespace(
        cfg=SimpleNamespace(
            maintenance_mode=False,
            admin_ids=(USER_ID,),
            model="test/model",
            hermes_input_price=0.30,
            hermes_output_price=1.20,
            allow_bots=False,
            # Router 出錯訊息會講自己個名（見 settings.self_name）。
            # 大肥鯨嗰個預設值，所以下面啲斷言照舊。
            self_name="本鯨",
        ),
        chain=chain,
        hermes=hermes,
        sessions=SimpleNamespace(notes=notes, get_group_profile=_no_profile),
        profiler=SimpleNamespace(schedule=lambda _cid: None),
        usage=SimpleNamespace(record=_noop_record),
        runaway=SimpleNamespace(allow=lambda _c: True),
        memory=SimpleNamespace(schedule=lambda **_kw: None),
        stickers=SimpleNamespace(menu=lambda: "", resolve=lambda _i: None),
        seen_conversations=set(),
        limiter=SimpleNamespace(check=lambda _uid: (True, 0)),
        access=SimpleNamespace(
            ensure=ensure, is_active=lambda _uid: True
        ),
        is_admin=lambda _uid: True,
        bot_id=BOT_ID,
        bot_name="大肥鯨",
        bot_username="fatwhale_bot",
        errors=0,
    )


def _run_group(message, svc, bot=None):
    update = SimpleNamespace(effective_message=message, effective_user=message.from_user)

    async def scenario():
        # patch 走「被指名」同「個群用得」兩個閘，集中測路由
        import dafeijing.router.handlers as h

        original_usable, original_addressed = h.group_usable, h.is_addressed_to_bot

        async def _true(*_a, **_k):
            return True

        h.group_usable, h.is_addressed_to_bot = _true, lambda *a, **k: True
        try:
            await on_group_message(update, _context(svc, bot))
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


# ── 圖片轉發 ────────────────────────────────────────────


def _photo_message(monkeypatch, bot=None, data_url: str = "data:image/jpeg;base64,AAA"):
    """令 pick_file 揀到一張相，並令下載回一張假圖。"""
    from dafeijing.core import media as media_mod
    from dafeijing.router import images as images_mod

    async def fake_collect(_bot, picked, _cfg):
        return [media_mod.PreparedImage(data_url, 100, 100, 10, picked.source)]

    monkeypatch.setattr(images_mod.media, "collect_media", fake_collect, raising=False)
    message = _message(message_id=500, bot=bot)
    message.photo = [SimpleNamespace(file_id="p", file_size=100, file_unique_id="u")]
    return message


def test_photo_reaches_hermes_as_an_inline_image(monkeypatch):
    hermes = FakeHermes()
    _run_group(_photo_message(monkeypatch), _services(FakeChain(), hermes))

    assert hermes.images[0] == ["data:image/jpeg;base64,AAA"]


def test_text_only_message_sends_no_images():
    hermes = FakeHermes()
    _run_group(_message(message_id=500), _services(FakeChain(), hermes))

    assert not hermes.images[0]


# ── 貼圖 ────────────────────────────────────────────────


class FakeStickers:
    def __init__(self, menu: str = "  1｜打招呼"):
        self._menu = menu

    def menu(self) -> str:
        return self._menu

    def resolve(self, index: int):
        if index == 1:
            return SimpleNamespace(
                file_id="sticker-1", meaning="打招呼", usage_hint="打招呼"
            )
        return None


def _sticker_svc(chain, hermes, stickers, bot=None):
    svc = _services(chain, hermes)
    svc.stickers = stickers
    return svc


def _mention_with_bot(message_id=500):
    """同一則訊息，連埋佢自己個 bot —— reply_markdown 用 message.get_bot()。"""
    bot = FakeBot()
    return _message(message_id=message_id, bot=bot), bot


def test_sticker_menu_is_attached_once_per_conversation():
    """清單每則都附嘅話，會不斷累積入 Hermes 嘅對話歷史，越傾越貴。"""
    stickers = FakeStickers()
    hermes = FakeHermes()
    svc = _sticker_svc(FakeChain({9001: CONV_A}), hermes, stickers)

    # 兩次都引用本鯨同一則 → 同一條對話
    _run_group(_message(message_id=950, reply_to=_reply_to_bot(9001)), svc)
    _run_group(_message(message_id=951, reply_to=_reply_to_bot(9001)), svc)

    assert hermes.calls[0][0] == hermes.calls[1][0] == CONV_A
    assert "<可用貼圖>" in hermes.calls[0][1], "第一則要附"
    assert "<可用貼圖>" not in hermes.calls[1][1], "第二則唔可以再附"


def test_sticker_menu_is_attached_for_each_new_conversation():
    stickers = FakeStickers()
    hermes = FakeHermes()
    svc = _sticker_svc(FakeChain(), hermes, stickers)

    _run_group(_message(message_id=500), svc)
    _run_group(_message(message_id=501), svc)

    assert hermes.calls[0][0] != hermes.calls[1][0], "兩條唔同對話"
    assert "<可用貼圖>" in hermes.calls[0][1]
    assert "<可用貼圖>" in hermes.calls[1][1]


def test_no_menu_block_when_the_library_is_empty():
    hermes = FakeHermes()
    _run_group(_message(), _sticker_svc(FakeChain(), hermes, FakeStickers(menu="")))

    assert "<可用貼圖>" not in hermes.calls[0][1]


def test_marker_in_the_reply_sends_the_sticker_and_is_stripped():
    chain = FakeChain()
    hermes = FakeHermes(text="好呀本鯨幫你睇[[貼圖:1]]")
    message, bot = _mention_with_bot()
    svc = _sticker_svc(chain, hermes, FakeStickers())

    _run_group(message, svc, bot)

    assert bot.stickers == [(CHAT, "sticker-1")]
    assert bot.sent[0][1] == "好呀本鯨幫你睇", "標記唔可以畀使用者見到"


def test_sticker_message_is_cached_so_the_chain_survives():
    """冇補快取嘅話，別人引用嗰張貼圖時條串會斷。"""
    chain = FakeChain()
    hermes = FakeHermes(text="睇下[[貼圖:1]]")
    message, bot = _mention_with_bot()
    svc = _sticker_svc(chain, hermes, FakeStickers())

    _run_group(message, svc, bot)

    cached = list(chain.replies)
    assert len(cached) == 2, "回覆一則、貼圖一則"
    assert cached[1]["message_id"] == 9002
    assert cached[1]["conversation"] == f"grp:{CHAT}:500"
    assert "貼圖" in cached[1]["text"], "貼圖嘅快取要有人睇得明嘅描述"


def test_unknown_sticker_number_sends_nothing_and_does_not_crash():
    """模型編錯號 —— 靜靜哋當冇，唔可以拋錯。"""
    hermes = FakeHermes(text="好[[貼圖:999]]")
    message, bot = _mention_with_bot()
    svc = _sticker_svc(FakeChain(), hermes, FakeStickers())

    _run_group(message, svc, bot)

    assert bot.stickers == []
    assert bot.sent[0][1] == "好"



# ── 存取閘 ──────────────────────────────────────────────
#
# 大肥鯨嘅規則：**群組靠「管理員在唔在個群」，唔逐個人查；私聊靠邀請碼。**
# 呢個分野要跟返 —— 喺群組加個人閘會令群友全部冇反應。


def _gate_svc(hermes, *, active: bool, admin: bool):
    svc = _services(FakeChain(), hermes)
    svc.is_admin = lambda _uid: admin

    async def _is_active(_uid: int) -> bool:
        return active

    async def _ensure(*_a, **_k) -> None:
        return None

    svc.access = SimpleNamespace(ensure=_ensure, is_active=_is_active)
    return svc


def test_group_member_is_served_even_when_not_active():
    """**大肥鯨冇喺群組擋人。** 佢嘅閘係 group_usable（管理員在場），
    唔係逐個人嘅邀請碼狀態。加咗個人閘會令群友全部冇反應。"""
    hermes = FakeHermes()
    _run_group(_message(message_id=500), _gate_svc(hermes, active=False, admin=False))

    assert len(hermes.calls) == 1, "群友應該照用得到"


def test_group_member_uses_the_group_scope_for_notes():
    """群友冇用戶列都要讀得到筆記（大肥鯨會即場 ensure 一列）。"""
    hermes = FakeHermes()
    _run_group(_message(message_id=500), _gate_svc(hermes, active=False, admin=False))

    assert hermes.calls[0][0] == f"grp:{CHAT}:500"


def test_a_stranger_dm_is_ignored():
    """私聊跟大肥鯨 —— 未啟用嘅人入唔到嚟。"""
    hermes = FakeHermes()
    bot = FakeBot()
    message = _message(chat_type=ChatType.PRIVATE, message_id=1, bot=bot)
    svc = _gate_svc(hermes, active=False, admin=False)

    asyncio.run(
        on_private_message(
            SimpleNamespace(
                effective_message=message, effective_user=message.from_user
            ),
            SimpleNamespace(bot_data={"services": svc}, bot=bot),
        )
    )

    assert hermes.calls == [], "唔應該叫 Hermes"
    assert bot.sent == [], "連拒絕都唔應該回 —— 免得變成回音壁"


def test_an_active_user_can_dm():
    hermes = FakeHermes()
    _run_private_dm(_gate_svc(hermes, active=True, admin=False))
    assert len(hermes.calls) == 1


def test_the_admin_can_dm_even_when_not_active():
    """管理員唔應該被自己個閘鎖住喺外面。"""
    hermes = FakeHermes()
    _run_private_dm(_gate_svc(hermes, active=False, admin=True))
    assert len(hermes.calls) == 1


def _run_private_dm(svc):
    bot = FakeBot()
    message = _message(chat_type=ChatType.PRIVATE, message_id=1, bot=bot)
    asyncio.run(
        on_private_message(
            SimpleNamespace(
                effective_message=message, effective_user=message.from_user
            ),
            SimpleNamespace(bot_data={"services": svc}, bot=bot),
        )
    )


# ── 引用串：要成條，而且次序要啱 ────────────────────────


def _entry(message_id: int, user_id: int, name: str, text: str) -> dict:
    return {
        "message_id": message_id,
        "user_id": user_id,
        "display_name": name,
        "text": text,
    }


def test_whole_chain_is_included_not_just_the_last_one():
    """用戶報嗰個：引用 @本鯨嗰時只睇到最尾嗰則。要成條串先夠。"""
    chain = [
        _entry(1, 111, "甲", "第一句"),
        _entry(2, 222, "乙", "第二句"),
        _entry(3, 333, "丙", "第三句"),
        _entry(500, USER_ID, "陳大文", "我嘅回應"),  # 觸發嗰則
    ]
    hermes = FakeHermes()
    _run_group(
        _message(text="我嘅回應", message_id=500, reply_to=_reply_to_user(3)),
        _services(FakeChain(chain=chain), hermes),
    )

    body = hermes.calls[0][1]
    assert "第一句" in body, "唔可以只附最尾嗰則"
    assert "第二句" in body
    assert "第三句" in body


def test_chain_is_in_chronological_order():
    """**次序唔可以亂** —— 亂咗因果會調轉，模型會當乙講嘅嘢早過甲。"""
    chain = [
        _entry(1, 111, "甲", "最早嗰句"),
        _entry(2, 222, "乙", "中間嗰句"),
        _entry(3, 333, "丙", "最尾嗰句"),
        _entry(500, USER_ID, "陳大文", "我嘅回應"),
    ]
    hermes = FakeHermes()
    _run_group(
        _message(text="我嘅回應", message_id=500, reply_to=_reply_to_user(3)),
        _services(FakeChain(chain=chain), hermes),
    )

    body = hermes.calls[0][1]
    assert body.index("最早嗰句") < body.index("中間嗰句") < body.index("最尾嗰句")
    assert body.index("最尾嗰句") < body.index("我嘅回應")


def test_every_chain_message_keeps_its_speaker():
    """一條串幾個人 —— 每則都要標返邊個講。"""
    chain = [
        _entry(1, 111, "甲", "甲講嘅"),
        _entry(2, 222, "乙", "乙講嘅"),
        _entry(500, USER_ID, "陳大文", "我嘅回應"),
    ]
    hermes = FakeHermes()
    _run_group(
        _message(text="我嘅回應", message_id=500, reply_to=_reply_to_user(2)),
        _services(FakeChain(chain=chain), hermes),
    )

    body = hermes.calls[0][1]
    assert "[甲|111]" in body
    assert "[乙|222]" in body


def test_the_trigger_is_not_duplicated():
    chain = [_entry(1, 111, "甲", "舊句"), _entry(500, USER_ID, "陳大文", "我嘅回應")]
    hermes = FakeHermes()
    _run_group(
        _message(text="我嘅回應", message_id=500, reply_to=_reply_to_user(1)),
        _services(FakeChain(chain=chain), hermes),
    )

    body = hermes.calls[0][1]
    assert body.count("我嘅回應") == 1, "觸發嗰句會另外送，唔好喺串入面再嚟一次"


def test_elision_marker_gets_no_speaker():
    """`resolve()` 摺疊中段插嘅標記唔係真訊息，唔應該標發言者。"""
    chain = [
        _entry(1, 111, "甲", "開頭"),
        {"message_id": -1, "user_id": None, "display_name": None, "text": "……（中間省略 5 則）"},
        _entry(3, 333, "丙", "收尾"),
        _entry(500, USER_ID, "陳大文", "我嘅回應"),
    ]
    hermes = FakeHermes()
    _run_group(
        _message(text="我嘅回應", message_id=500, reply_to=_reply_to_user(3)),
        _services(FakeChain(chain=chain), hermes),
    )

    body = hermes.calls[0][1]
    assert "中間省略 5 則" in body
    assert "[None|" not in body


def test_media_only_chain_entries_are_skipped():
    chain = [
        {"message_id": 1, "user_id": 111, "display_name": "甲", "text": ""},
        _entry(2, 222, "乙", "有字"),
        _entry(500, USER_ID, "陳大文", "我嘅回應"),
    ]
    hermes = FakeHermes()
    _run_group(
        _message(text="我嘅回應", message_id=500, reply_to=_reply_to_user(2)),
        _services(FakeChain(chain=chain), hermes),
    )

    assert "有字" in hermes.calls[0][1]


def test_falls_back_to_the_immediate_parent_when_the_cache_misses():
    """重啟之後第一次見到 —— 快取追唔到，至少附返最尾嗰則。"""
    hermes = FakeHermes()
    parent = _reply_to_user(message_id=300, text="快取冇嘅一句")
    _run_group(
        _message(text="你覺得點", message_id=950, reply_to=parent),
        _services(FakeChain(chain=[]), hermes),
    )

    assert "快取冇嘅一句" in hermes.calls[0][1]


def test_quoting_the_bot_attaches_no_chain():
    """續同一條對話 —— Hermes 歷史已經有齊，再送會重複。"""
    chain = [_entry(1, 111, "甲", "久遠嗰句"), _entry(9001, BOT_ID, "大肥鯨", "本鯨答過")]
    hermes = FakeHermes()
    parent = _reply_to_bot(9001)
    parent.text = "本鯨答過"
    _run_group(
        _message(text="咁你覺得點", message_id=950, reply_to=parent),
        _services(FakeChain({9001: CONV_A}, chain=chain), hermes),
    )

    body = hermes.calls[0][1]
    assert "久遠嗰句" not in body
    assert "本鯨答過" not in body


# ── 防失控 ──────────────────────────────────────────────


def _bot_message(*, is_bot=True, message_id=500):
    message = _message(message_id=message_id)
    message.from_user = SimpleNamespace(
        id=777, full_name="另一隻 bot", username="other_bot", is_bot=is_bot
    )
    return message


def test_a_message_from_another_bot_is_ignored():
    """**呢個就係斷開循環嘅位。** 另一個 bot 引用本鯨嘅回覆 ——
    `is_addressed_to_bot` 當佢係被指名，但佢唔係人。"""
    hermes = FakeHermes()
    svc = _services(FakeChain({9001: CONV_A}), hermes)
    parent = _reply_to_bot(9001)

    _run_group(_bot_message(message_id=950), svc)

    assert hermes.calls == [], "唔可以叫 Hermes"
    assert hermes.calls == [], "亦唔可以有任何快取動作"


def test_a_human_message_is_unaffected():
    """呢道閘唔可以誤擋真人。"""
    hermes = FakeHermes()
    _run_group(_message(message_id=500), _services(FakeChain(), hermes))
    assert len(hermes.calls) == 1


def test_allow_bots_lets_it_through_when_asked():
    """想試 bot-to-bot 嘅話可以開，但預設係關。"""
    hermes = FakeHermes()
    svc = _services(FakeChain({9001: CONV_A}), hermes)
    svc.cfg.allow_bots = True

    _run_group(_bot_message(message_id=950), svc)

    assert len(hermes.calls) == 1


def test_runaway_guard_blocks_before_any_work():
    """剎停要喺下載媒體之前 —— 失控嗰陣最貴嘅係 Hermes 嗰一 call。"""
    hermes = FakeHermes()
    svc = _services(FakeChain(), hermes)
    svc.runaway = SimpleNamespace(allow=lambda _c: False)

    _run_group(_message(message_id=500), svc)

    assert hermes.calls == []


def test_runaway_guard_is_asked_about_the_conversation():
    """要用對話名做鍵 —— 一條串失控唔應該拖累另一條。"""
    asked: list[str] = []
    hermes = FakeHermes()
    svc = _services(FakeChain(), hermes)
    svc.runaway = SimpleNamespace(allow=lambda c: (asked.append(c), True)[1])

    _run_group(_message(message_id=500), svc)

    assert asked == [f"grp:{CHAT}:500"]


def test_private_bot_sender_is_ignored():
    hermes = FakeHermes()
    bot = FakeBot()
    message = _bot_message(message_id=1)
    message.chat = SimpleNamespace(id=CHAT, type=ChatType.PRIVATE)
    svc = _services(FakeChain(), hermes)

    asyncio.run(
        on_private_message(
            SimpleNamespace(
                effective_message=message, effective_user=message.from_user
            ),
            SimpleNamespace(bot_data={"services": svc}, bot=bot),
        )
    )

    assert hermes.calls == []
