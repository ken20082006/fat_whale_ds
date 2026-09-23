"""訊息合併（debounce）。

Telegram 使用者習慣把一句話拆成好幾條送出。若每條都呼叫一次模型，
不但浪費 token，回答也會被切碎。這裡讓第一則訊息當「leader」，
等待短暫靜默後，把這段期間同一對話的所有訊息合併成一輪。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .media import PreparedImage

logger = logging.getLogger(__name__)

# 一次合併最多帶幾張圖。太多圖會讓成本與延遲同時上升，也超出模型能有效比較的範圍。
MAX_IMAGES = 4


@dataclass
class _Buffer:
    parts: list[str] = field(default_factory=list)
    images: list["PreparedImage"] = field(default_factory=list)


class Debouncer:
    def __init__(self, delay_seconds: float) -> None:
        self._delay = delay_seconds
        self._buffers: dict[str, _Buffer] = {}
        self._leaders: set[str] = set()
        self._lock = asyncio.Lock()

    async def gather(
        self,
        key: str,
        text: str,
        images: list["PreparedImage"] | None = None,
    ) -> tuple[str, list["PreparedImage"]] | None:
        """合併同一對話的連續訊息。

        回傳 (合併後文字, 圖片列表) 給 leader；非 leader 回傳 None，
        代表這則訊息已被併入前一輪，呼叫端應直接結束。
        """
        async with self._lock:
            if key in self._leaders:
                buffer = self._buffers.get(key)
                if buffer is not None:
                    buffer.parts.append(text)
                    for image in images or []:
                        if len(buffer.images) < MAX_IMAGES:
                            buffer.images.append(image)
                return None

            self._leaders.add(key)
            self._buffers[key] = _Buffer(
                parts=[text], images=list(images or [])[:MAX_IMAGES]
            )

        try:
            await asyncio.sleep(self._delay)
            async with self._lock:
                buffer = self._buffers.pop(key, None)
            if buffer is None:
                return None
            merged = "\n".join(part for part in buffer.parts if part.strip())
            if len(buffer.parts) > 1:
                logger.debug("合併 %d 則訊息（%s）", len(buffer.parts), key)
            return merged, buffer.images
        finally:
            async with self._lock:
                self._leaders.discard(key)

    async def drain(self) -> None:
        """關機時清空緩衝，避免殘留。"""
        async with self._lock:
            self._buffers.clear()
            self._leaders.clear()
