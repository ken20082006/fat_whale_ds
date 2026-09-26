"""貼圖 —— 畀模型揀，同埋將佢揀嗰張送出。

模型唔可能每次回覆都睇一百幾十張圖去揀，所以標註係離線做
（`scripts/label_stickers.py`），執行期只放一份精選清單，模型用
`[[貼圖:編號]]` 指明。挑選同送出嘅邏輯全部複用大肥鯨 `core/stickers.py`。

**清單放喺邊**：大肥鯨放喺 system prompt，屬快取前綴，邊際成本近零。
Router 改唔到 Hermes 嘅 system prompt（`SOUL.md` 係檔案），所以唯有放喺
請求度 —— 但**只喺開新對話嗰陣放一次**。每則都放嘅話，清單會不斷累積入
Hermes 嘅對話歷史，越傾越貴。
"""

from __future__ import annotations

import logging

from ..core.stickers import extract_marker

logger = logging.getLogger(__name__)

# 文字沿用大肥鯨 core/persona.py:288-303 —— 嗰段係實測調出嚟嘅
# （頻率、時機、唔好自己編編號），唔好求其改。
_HEADER = """\
（以下係一組可用貼圖，用嚟表達文字講唔清楚嘅情緒。想用嗰陣，
　喺回覆最尾加 [[貼圖:編號]]，系統會將嗰張貼圖一併送出。

　該用嘅時機：打招呼、對方玩緊、情緒明顯（開心、累、委屈、無言、想吐槽）。
　頻率：大約每五到十則用一次。唔好連續兩則都用，亦唔好整段都唔敢用。
　唔好用嘅時機：對方認真問事、忙緊、焦慮、講緊嚴肅嘅事。

　編號只可以取自下面清單，唔好自己編。標記放喺最尾，前後唔好加嘢。）"""


def menu_block(menu: str) -> str:
    """砌貼圖清單區塊。冇貼圖就回空字串。"""
    if not menu.strip():
        return ""
    return f"{_HEADER}\n<可用貼圖>\n{menu}\n</可用貼圖>"


def split_marker(text: str) -> tuple[str, int | None]:
    """抽出 `[[貼圖:編號]]` 並由回覆移除。

    標記唔可以留喺訊息度畀使用者見到。實作照用大肥鯨嗰個 ——
    佢容錯全角冒號同空白。
    """
    cleaned, index = extract_marker(text)
    if index is not None:
        logger.debug("模型揀咗貼圖 #%d", index)
    return cleaned, index