"""人設 prompt 的組裝。

`config/persona.md` 只放角色設定本身，不含任何樣板語法 —— 改人設不必懂程式。
動態的部分（濃度、對象、長期記憶、摘要）由這裡在執行期附加。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# 人設濃度的調節說明。使用者可用 /vibe 切換。
VIBE_INSTRUCTIONS: dict[str, str] = {
    "low": (
        "演出濃度：**低**。只保留最基本的稱謂與語氣，其餘一律中性、專業、直接。"
        "除非對方主動玩梗，否則不加任何角色化描寫。"
    ),
    "mid": (
        "演出濃度：**中**（預設）。依照上述克制規則做標準演出，多數回覆應完全沒有角色化描寫。"
    ),
    "high": (
        "演出濃度：**高**。可以較多角色化演出與吐槽，但仍不得妨礙答案本身，"
        "且技術性與長篇回答依然要收斂。"
    ),
}

DEFAULT_VIBE = "mid"

_FALLBACK_BODY = "你是一個樂於助人的助理。回答要準確、簡潔、直接。"

# 放在程式而非 persona.md：這是媒體處理的結構性行為，即使換一份完全不同的角色設定
# 也該成立。寫死在這裡可以保證不會因為改人設而失效。
_MEDIA_RULE = """\

### 圖片與貼圖

對方傳圖片或貼圖過來時，那是在對你說話，不是在出題。順著他的意思回應就好 ——
不要描述畫面，不要複述圖上的文字，也不要猜他想問什麼。
一張哭臉貼圖要的是回應，不是一份圖片說明。

只有在他明確要你看圖時（「這張圖是什麼」「幫我睇下」「圖入面寫咩」之類），
才認真讀圖回答。那種時候要好好看，不要敷衍。
"""


@dataclass(frozen=True)
class PersonaContext:
    """組裝 system prompt 所需的動態資訊。"""

    vibe: str = DEFAULT_VIBE
    display_name: str | None = None
    notes: list[str] = field(default_factory=list)
    summary: str | None = None
    is_group: bool = False


class Persona:
    def __init__(self, body: str, source: Path | None = None) -> None:
        self.body = body.strip()
        self.source = source

    # ── 載入 ────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path) -> "Persona":
        resolved = _read_first_existing(path)
        if resolved is None:
            logger.warning(
                "找不到人設檔案 %s，也未找到 %s。將使用內建的最小預設。",
                path,
                path.with_name("persona.example.md"),
            )
            return cls(_FALLBACK_BODY)

        body = resolved.read_text(encoding="utf-8")
        if resolved != path:
            logger.warning(
                "找不到 %s，已退回使用範本 %s。請複製為 %s 後改寫。",
                path,
                resolved,
                path,
            )
        else:
            logger.info("已載入人設：%s（%d 字）", resolved, len(body))
        return cls(body, resolved)

    def reload(self, path: Path) -> None:
        fresh = Persona.load(path)
        self.body = fresh.body
        self.source = fresh.source

    # ── 組裝 ────────────────────────────────────────────

    def build(self, ctx: PersonaContext) -> str:
        blocks = [self.body, _MEDIA_RULE, "\n\n---\n\n## 本次對話的附加條件\n"]

        vibe = ctx.vibe if ctx.vibe in VIBE_INSTRUCTIONS else DEFAULT_VIBE
        blocks.append(f"\n### 演出濃度\n{VIBE_INSTRUCTIONS[vibe]}\n")

        blocks.append(f"\n### 對話對象\n{ctx.display_name or '（未知）'}\n")

        blocks.append("\n### 場合\n")
        if ctx.is_group:
            blocks.append(
                "你在一個群組裡，而且是被指名或被回覆的那一方。"
                "只回應與這條引用串相關的內容，不要評論群組裡其他人的閒聊。"
                "回覆要比私聊更短。\n"
            )
        else:
            blocks.append("這是私聊。\n")

        if ctx.notes:
            blocks.append("\n### 長期記憶（跨對話保存）\n")
            for note in ctx.notes:
                blocks.append(f"- {note}\n")

        if ctx.summary:
            blocks.append("\n### 較早對話的摘要\n")
            blocks.append(f"{ctx.summary}\n")

        return "".join(blocks)


def _read_first_existing(path: Path) -> Path | None:
    """依序嘗試 path、persona.example.md。回傳實際讀到的檔案。"""
    candidates = [path, path.with_name("persona.example.md")]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None
