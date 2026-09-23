"""貼圖庫：讓 bot 回覆時能偶爾附一張貼圖。

讀圖可以直接把圖送給模型看，但**挑選**不行 —— 總不能每次回覆都把一百多張圖
塞進去讓它選。所以標註是離線做的（scripts/label_stickers.py），執行期只放一份
精選清單在提示裡，模型用 [[貼圖:編號]] 指明要哪張。

清單在 system prompt 裡，屬於快取前綴，所以後續請求的邊際成本接近零。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .util import truncate

logger = logging.getLogger(__name__)

# 模型輸出的標記。容錯全角冒號與空白。
MARKER = re.compile(r"\[\[\s*貼圖\s*[:：]\s*(\d+)\s*\]\]")

# 清單每行保留多少字。太長會讓提示膨脹，太短又分不出差異。
_HINT_LENGTH = 30


@dataclass(frozen=True)
class StickerEntry:
    index: int
    file_id: str
    meaning: str
    usage_hint: str

    def menu_line(self) -> str:
        return f"  {self.index}｜{truncate(self.usage_hint, _HINT_LENGTH)}"


class StickerLibrary:
    def __init__(self, cfg) -> None:
        self._cfg = cfg
        self._entries: list[StickerEntry] = []
        self._by_index: dict[int, StickerEntry] = {}

    async def load(self, db) -> None:
        """從資料庫讀出精選清單。featured 由人工或腳本標記。"""
        rows = await db.fetchall(
            "SELECT rowid, file_id, meaning, usage_hint FROM stickers "
            "WHERE featured = 1 AND file_id IS NOT NULL AND file_id != '' "
            "ORDER BY rowid"
        )
        self._entries = [
            StickerEntry(
                index=row["rowid"],
                file_id=row["file_id"],
                meaning=row["meaning"] or "",
                usage_hint=row["usage_hint"] or row["meaning"] or "",
            )
            for row in rows
        ]
        self._by_index = {entry.index: entry for entry in self._entries}
        logger.info("貼圖庫載入 %d 張精選", len(self._entries))

    @property
    def available(self) -> bool:
        return bool(self._entries)

    def menu(self) -> str:
        return "\n".join(entry.menu_line() for entry in self._entries)

    def resolve(self, index: int) -> StickerEntry | None:
        return self._by_index.get(index)


def extract_marker(text: str) -> tuple[str, int | None]:
    """把標記從回覆裡拿掉，回傳 (清理後文字, 編號)。

    標記不能留在訊息裡給使用者看到。
    """
    match = MARKER.search(text)
    if match is None:
        return text, None

    cleaned = MARKER.sub("", text)
    # 移除標記後常留下多餘空行
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, int(match.group(1))
