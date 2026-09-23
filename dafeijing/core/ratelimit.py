"""每人每分鐘的請求上限。

純記憶體實作，重啟即歸零 —— 這對防手滑與防連發已經足夠，
真正的成本控制靠 usage_log 的長期記錄。
"""

from __future__ import annotations

import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self._limit = per_minute
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    def check(self, key: int) -> tuple[bool, int]:
        """回傳 (是否放行, 需等待秒數)。"""
        now = time.monotonic()
        window = self._hits[key]

        while window and now - window[0] > 60.0:
            window.popleft()

        if len(window) >= self._limit:
            wait = int(60.0 - (now - window[0])) + 1
            return False, max(wait, 1)

        window.append(now)
        return True, 0

    def reset(self, key: int) -> None:
        self._hits.pop(key, None)
