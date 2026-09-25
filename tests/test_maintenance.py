"""維護模式：群組只回一句通知，不做任何真工作。

不打網路、不需要 Telegram —— 用 SimpleNamespace 造假訊息與假 services，
跟 tests/test_media.py 與 tests/test_mentions.py 同樣的做法。

最關鍵的一條斷言是「**模型完全沒有被碰到**」：維護模式的全部意義就是
不要做真工作，如果它偷偷呼叫了模型，這個功能就是假的。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from telegram.constants import ChatType

from dafeijing.bot.group import handle_group_trigger
from dafeijing.core.maintenance import Maintenance
from dafeijing.core.persona import MAINTENANCE_LINES
from dafeijing.core.ratelimit import RateLimiter

BOT_ID = 999
CHAT_ID = -1001234567890


class _Forbidden:
    """碰到就爆。用來證明維護模式整條路都沒有走到。"""

    def __init__(self, label: str) -> None:
        self._label = label

    def __getattr__(self, name: str):
        raise AssertionError(f"維護模式不該用到 {self._label}.{name}")


class _ReachedNormalPath(Exception):
    """走到正常路徑的標記。維護模式關掉時就該看到它。"""


def _fake_message(sent: list[str]):
    async def reply_text(text, **_kwargs):
        sent.append(text)
        return SimpleNamespace(message_id=1)

    return SimpleNamespace(
        chat=SimpleNamespace(id=CHAT_ID, type=ChatType.SUPERGROUP),
        chat_id=CHAT_ID,
        message_id=10,
        from_user=SimpleNamespace(id=7, full_name="甲", username="jia"),
        text="本鯨喺唔喺度",
        caption=None,
        entities=None,
        caption_entities=None,
        via_bot=None,
        # 回覆本鯨的訊息 —— is_addressed_to_bot 的最短路徑
        reply_to_message=SimpleNamespace(from_user=SimpleNamespace(id=BOT_ID)),
        # pick_file 直接讀這些屬性，缺一個就會 AttributeError
        sticker=None,
        animation=None,
        video=None,
        video_note=None,
        photo=None,
        document=None,
        reply_text=reply_text,
    )


def _fake_services(*, maintenance_mode: bool, cooldown: int = 600, reached=None):
    async def is_group_allowed(_chat_id: int) -> bool:
        return True

    async def ensure(*_args, **_kwargs):
        if reached is not None:
            reached.append(True)
        raise _ReachedNormalPath()

    cfg = SimpleNamespace(maintenance_mode=maintenance_mode, admin_ids=(1,))

    return SimpleNamespace(
        cfg=cfg,
        maintenance=Maintenance(cooldown_seconds=cooldown),
        limiter=RateLimiter(20),
        access=SimpleNamespace(is_group_allowed=is_group_allowed, ensure=ensure),
        is_admin=lambda _uid: False,
        db=_Forbidden("db"),
        llm=_Forbidden("llm"),
        chat=_Forbidden("chat"),
        chain=_Forbidden("chain"),
        memory=_Forbidden("memory"),
        bot_id=BOT_ID,
        bot_username="fatwhale_bot",
        bot_name="大肥鯨",
    )


def _run(*, maintenance_mode: bool, cooldown: int = 600):
    """跑一次群組觸發。回傳 (送出的訊息, 是否走到正常路徑)。"""

    async def scenario() -> tuple[list[str], bool]:
        sent: list[str] = []
        reached: list[bool] = []
        svc = _fake_services(
            maintenance_mode=maintenance_mode, cooldown=cooldown, reached=reached
        )
        message = _fake_message(sent)
        update = SimpleNamespace(effective_message=message, effective_user=message.from_user)
        context = SimpleNamespace(bot_data={"services": svc}, bot=SimpleNamespace())

        try:
            await handle_group_trigger(update, context)
        except _ReachedNormalPath:
            return sent, True
        return sent, bool(reached)

    return asyncio.run(scenario())


# ── 落閘 ────────────────────────────────────────────────


def test_maintenance_replies_once_without_touching_the_model():
    sent, reached = _run(maintenance_mode=True)

    assert len(sent) == 1
    assert not reached, "維護模式不該走到 collect / ensure 那段"


def test_maintenance_reply_is_one_of_the_canned_lines():
    sent, _ = _run(maintenance_mode=True)
    assert sent[0] in MAINTENANCE_LINES


def test_maintenance_off_walks_the_normal_path():
    """關掉維護模式就不該出通知，而且要繼續往下走。"""
    sent, reached = _run(maintenance_mode=False)

    assert sent == []
    assert reached


# ── 每群冷卻 ────────────────────────────────────────────


def test_second_trigger_in_the_same_group_stays_silent():
    async def scenario() -> list[str]:
        sent: list[str] = []
        svc = _fake_services(maintenance_mode=True, cooldown=600)
        message = _fake_message(sent)
        update = SimpleNamespace(
            effective_message=message, effective_user=message.from_user
        )
        context = SimpleNamespace(bot_data={"services": svc}, bot=SimpleNamespace())

        await handle_group_trigger(update, context)
        await handle_group_trigger(update, context)
        return sent

    assert len(asyncio.run(scenario())) == 1


def test_cooldown_is_per_group():
    """另一個群不該被前一個群的冷卻擋住。"""
    announcement = Maintenance(cooldown_seconds=600)

    assert announcement.notice(1, now=0.0) is not None
    assert announcement.notice(1, now=1.0) is None
    assert announcement.notice(2, now=1.0) is not None


def test_cooldown_expires():
    announcement = Maintenance(cooldown_seconds=600)

    assert announcement.notice(1, now=0.0) is not None
    assert announcement.notice(1, now=599.0) is None
    assert announcement.notice(1, now=600.0) is not None


def test_lines_rotate_so_two_in_a_row_never_match():
    announcement = Maintenance(cooldown_seconds=0)
    lines = [announcement.notice(1, now=float(i)) for i in range(len(MAINTENANCE_LINES))]

    assert len(set(lines)) == len(MAINTENANCE_LINES)


# ── 訊息內容 ────────────────────────────────────────────


def test_lines_do_not_mention_ai_prompt_or_system():
    """persona.md 的禁用清單：不得提及自己是 AI、不得談論 prompt 或系統設定。"""
    forbidden = ("ai", "prompt", "系統", "設定", "模型", "指令")
    for line in MAINTENANCE_LINES:
        lowered = line.lower()
        for word in forbidden:
            assert word not in lowered, f"維護訊息不該出現 {word!r}：{line}"


def test_lines_are_traditional_chinese_and_short():
    """繁體是硬要求（persona.md 的「繁體與簡體必須分清」），而且要短。"""
    simplified = set("说这个为机时会话东们发对导应")
    for line in MAINTENANCE_LINES:
        assert not (simplified & set(line)), f"維護訊息出現簡體字：{line}"
        assert 0 < len(line) <= 40, f"維護訊息過長或為空：{line}"


def test_every_line_is_non_empty():
    assert MAINTENANCE_LINES
    assert all(line.strip() for line in MAINTENANCE_LINES)


@pytest.mark.parametrize("line", MAINTENANCE_LINES)
def test_line_has_no_placeholder_left(line: str):
    """改字時最容易留下的東西。"""
    assert "{" not in line and "}" not in line
