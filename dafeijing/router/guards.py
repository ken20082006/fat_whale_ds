"""兩道防失控嘅閘。

## 為什麼要

**兩個 bot 互相回覆可以永遠停唔到。** 呢個唔係假設 —— Hermes 自己個
codebase 有同一道閘，註釋寫得好白：

> another bot must explicitly @mention us, its quote-replies and plain chatter
> do not count (**two bots answering each other's replies never stop otherwise**)
> —— `adapter.py:6113`

而循環嘅入口就喺「回覆本鯨就算被指名」嗰條規則：另一個 bot 引用本鯨 →
Router 當佢係同本鯨講嘢 → 回覆 → 佢又引用返 → 冇完。每次來回都係一次
Hermes 呼叫（實測 ~11k input tokens）。

## 閘一：完全唔理其他 bot

**最安全、最少驚喜。** 唔理佢係 @ 定引用定隨口講，一律唔應。

點解唔跟 Hermes 嗰套（允許明確 @）？因為「明確 @」只係將循環變慢，
冇斷開佢 —— 兩個 bot 只要互相 @ 一次就照樣起飛。而群組裡面正常唔會需要
同另一個 bot 傾偈。

## 閘二：對話層面嘅失控剎停

userbot 係**用戶帳號**，Telegram 當佢係人 —— 閘一捉唔到。所以加多一層：
同一條對話短時間內太多次呼叫就剎停，並且大聲 log。

正常傾偈撞唔到（今日最密都係幾分鐘幾次），但任何失控都會即刻斷。
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque

logger = logging.getLogger(__name__)


def is_bot_sender(message) -> bool:
    """呢則訊息係咪另一個 bot 發嘅。

    Telegram 嘅 Bot API 本身唔會將其他 bot 嘅訊息送過嚟，但：
    - 呢個規則有例外（Hermes 就係為咗噉寫咗同款嘅閘）
    - 引用嗰則係**獨立送出**嘅，`reply_to_message.from_user.is_bot` 睇得到

    所以照樣要擋。頻道身份發言（`sender_chat`）亦當唔係人 —— 群組匿名
    管理員係例外，但佢極少見，而誤擋嘅代價遠低過失控嘅代價。
    """
    sender = getattr(message, "from_user", None)
    if sender is not None and getattr(sender, "is_bot", False):
        return True
    sender_chat = getattr(message, "sender_chat", None)
    return sender_chat is not None and getattr(sender_chat, "type", None) == "channel"


class RunawayGuard:
    """同一條對話短時間內太多次呼叫就剎停。

    純記憶體，重啟歸零 —— 同 `core/ratelimit.py` 一樣嘅取捨：呢個係防失控，
    唔係需要持久化嘅狀態。
    """

    def __init__(self, max_calls: int, window_seconds: float) -> None:
        self._max = max_calls
        self._window = window_seconds
        self._calls: dict[str, deque[float]] = defaultdict(deque)
        self._warned: set[str] = set()

    def allow(self, conversation: str, now: float | None = None) -> bool:
        """呢條對話可唔可以再叫一次。`now` 只為咗測試可以控制時間。"""
        current = time.monotonic() if now is None else now
        window = self._calls[conversation]

        while window and current - window[0] > self._window:
            window.popleft()

        if len(window) >= self._max:
            # 只 log 一次 —— 失控嗰陣每則都 log 會將日誌灌爆。
            if conversation not in self._warned:
                self._warned.add(conversation)
                logger.error(
                    "對話 %s 喺 %.0f 秒內超過 %d 次呼叫，剎停。"
                    "通常代表有嘢失控（bot 循環、userbot、或者有人洗版）",
                    conversation,
                    self._window,
                    self._max,
                )
            return False

        window.append(current)
        self._warned.discard(conversation)
        return True

    def reset(self, conversation: str) -> None:
        self._calls.pop(conversation, None)
        self._warned.discard(conversation)