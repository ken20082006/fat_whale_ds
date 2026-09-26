"""防失控 —— 兩個 bot 互相回覆可以永遠停唔到。

Hermes 自己個 codebase 有同一道閘，註釋寫得好白：

> another bot must explicitly @mention us, its quote-replies and plain chatter
> do not count (**two bots answering each other's replies never stop otherwise**)

我哋嘅做法更硬：**完全唔理其他 bot**。「明確 @」只係將循環變慢，冇斷開佢。
"""

from __future__ import annotations

from types import SimpleNamespace

from dafeijing.router.guards import RunawayGuard, is_bot_sender


# ── 邊個算係 bot ────────────────────────────────────────


def _message(*, is_bot=False, sender_chat_type=None):
    sender_chat = (
        SimpleNamespace(type=sender_chat_type) if sender_chat_type else None
    )
    return SimpleNamespace(
        from_user=SimpleNamespace(id=1, full_name="甲", is_bot=is_bot),
        sender_chat=sender_chat,
    )


def test_a_human_is_not_a_bot():
    assert not is_bot_sender(_message())


def test_another_bot_is_detected():
    assert is_bot_sender(_message(is_bot=True))


def test_a_channel_identity_is_not_a_person():
    """頻道身份發言當唔係人 —— 誤擋嘅代價遠低過失控嘅代價。"""
    assert is_bot_sender(_message(sender_chat_type="channel"))


def test_a_group_sender_chat_is_not_blocked():
    """群組匿名管理員 —— `sender_chat.type` 係 supergroup，唔應該當 bot。"""
    assert not is_bot_sender(_message(sender_chat_type="supergroup"))


def test_missing_fields_do_not_crash():
    """真實嘅 telegram.Message 一定有，但唔應該靠佢。"""
    assert not is_bot_sender(SimpleNamespace())


# ── 失控剎停 ────────────────────────────────────────────


def test_normal_use_passes():
    guard = RunawayGuard(max_calls=30, window_seconds=300.0)
    assert all(guard.allow("grp:1", now=float(i)) for i in range(30))


def test_exceeding_the_cap_blocks():
    guard = RunawayGuard(max_calls=3, window_seconds=300.0)
    assert guard.allow("grp:1", now=0.0)
    assert guard.allow("grp:1", now=1.0)
    assert guard.allow("grp:1", now=2.0)
    assert not guard.allow("grp:1", now=3.0), "第 4 次應該剎停"


def test_the_window_slides():
    """窗口過咗就放行返 —— 唔係永久封鎖。"""
    guard = RunawayGuard(max_calls=2, window_seconds=10.0)
    assert guard.allow("grp:1", now=0.0)
    assert guard.allow("grp:1", now=1.0)
    assert not guard.allow("grp:1", now=5.0)
    assert guard.allow("grp:1", now=11.0), "0.0 嗰次已經滑出窗口"


def test_conversations_are_counted_separately():
    """一條對話失控唔應該拖累另一條。"""
    guard = RunawayGuard(max_calls=1, window_seconds=300.0)
    assert guard.allow("grp:1", now=0.0)
    assert guard.allow("grp:2", now=0.0)
    assert not guard.allow("grp:1", now=1.0)
    assert not guard.allow("grp:2", now=1.0)


def test_recovers_after_the_window():
    guard = RunawayGuard(max_calls=1, window_seconds=5.0)
    assert guard.allow("grp:1", now=0.0)
    assert not guard.allow("grp:1", now=1.0)
    assert guard.allow("grp:1", now=6.0)


def test_reset_clears_a_conversation():
    guard = RunawayGuard(max_calls=1, window_seconds=300.0)
    assert guard.allow("grp:1", now=0.0)
    assert not guard.allow("grp:1", now=1.0)
    guard.reset("grp:1")
    assert guard.allow("grp:1", now=2.0)


def test_it_warns_but_does_not_flood_the_log(caplog):
    """失控嗰陣每則都 log 會將日誌灌爆。"""
    import logging

    guard = RunawayGuard(max_calls=1, window_seconds=300.0)
    guard.allow("grp:1", now=0.0)
    with caplog.at_level(logging.ERROR):
        for i in range(1, 20):
            guard.allow("grp:1", now=float(i))

    warnings = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(warnings) == 1, "只可以 log 一次"
    assert "剎停" in warnings[0].getMessage()


def test_the_warning_names_what_it_usually_means():
    """要講清楚通常代表咩，唔係嘅話睇 log 都唔知發生咩事。"""
    guard = RunawayGuard(max_calls=1, window_seconds=300.0)
    guard.allow("grp:1", now=0.0)
    guard.allow("grp:1", now=1.0)
    assert "grp:1" in guard._warned  # noqa: SLF001


# ── 預設值要合理 ────────────────────────────────────────


def test_defaults_do_not_bite_normal_conversation():
    """正常傾偈撞唔到 —— 否則呢道閘會變成人為故障。"""
    from dafeijing.settings import Settings

    cfg = Settings(
        telegram_bot_token="1:a", openrouter_api_key="x"
    )
    assert cfg.allow_bots is False, "預設一定要擋 bot"
    assert cfg.conversation_max_calls >= 20, "正常傾偈唔應該撞到"
    assert cfg.conversation_window_seconds >= 60, "窗口太短會誤剎"
