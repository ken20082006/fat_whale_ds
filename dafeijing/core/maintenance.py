"""維護模式的群組通知。

開啟維護模式之後，群組被指名也不做任何真工作，只回一句人設語氣的
「調整緊」。這裡負責兩件事：**輪流揀句**（避免連續兩則同一招）與
**每群冷卻**（避免整個群每個人 @ 一次就洗版）。

純記憶體，重啟即歸零 —— 跟 core/ratelimit.py 同樣的取捨：這只是防手滑
與防洗版，不是需要持久化的狀態。
"""

from __future__ import annotations

import time

from .persona import MAINTENANCE_LINES


class Maintenance:
    """維護模式的群組通知。不碰 Telegram，也不知道訊息內容。"""

    def __init__(self, cooldown_seconds: int) -> None:
        self._cooldown = cooldown_seconds
        self._last: dict[int, float] = {}
        self._cursor = 0

    def notice(self, chat_id: int, now: float | None = None) -> str | None:
        """回傳這一則該講的維護訊息；冷卻中則回 None（代表這次不出聲）。

        `now` 只是為了測試能控制時間，正式路徑不傳。
        """
        current = time.monotonic() if now is None else now
        last = self._last.get(chat_id)
        if last is not None and current - last < self._cooldown:
            return None

        self._last[chat_id] = current
        line = MAINTENANCE_LINES[self._cursor % len(MAINTENANCE_LINES)]
        self._cursor += 1
        return line

    def reset(self, chat_id: int) -> None:
        self._last.pop(chat_id, None)
