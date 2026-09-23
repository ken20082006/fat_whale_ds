"""自動長期記憶。

每輪對話後，背景跑一次抽取：從這一來一往裡找出「關於這個人、值得長期記住的事實」，
寫進該場合的筆記。使用者不必手動 /remember。

成本考量：抽取走便宜的 utility 模型、關閉推理，且只在訊息有實質內容時才跑。
閒聊一兩句就跳過，所以多數訊息不會產生額外呼叫。
"""

from __future__ import annotations

import asyncio
import logging
import re

from .session import SessionManager
from .util import truncate

logger = logging.getLogger(__name__)

# 條列符號與「1. 」這類編號。只砍這些，不要連事實開頭的數字一起砍掉。
_MARKER = re.compile(r"^\s*(?:[-•*]|\d+[.)])\s+")
_NOTHING = re.compile(r"^[（(]?\s*[無无]\s*[）)]?[。.]?$")

_PROMPT = """\
你在維護一份關於某個人的長期筆記，這份筆記會在之後的對話中提供給助理參考。

從下面這段對話裡，找出**關於這位使用者本人**、值得長期記住的事實。

值得記：
- 身分與背景：名字、職業、居住地、慣用語言
- 長期偏好：喜歡什麼、討厭什麼、習慣怎麼做事
- 持續進行的事：專案、目標、正在學的東西
- 明確要求記住的事

不要記：
- 一次性的問題、當下的情緒、閒聊
- 已經在既有筆記裡的事
- 助理自己說過的話
- 任何推測或不確定的內容

既有筆記：
{existing}

對話：
{exchange}

若沒有新的、值得長期記住的事實，只回覆「無」。
否則每行一則事實，最多三則。直接寫事實，不要編號、不要前言、不要解釋。
"""


class MemoryExtractor:
    def __init__(self, cfg, sessions: SessionManager, llm) -> None:
        self._cfg = cfg
        self._sessions = sessions
        self._llm = llm
        self._tasks: set[asyncio.Task] = set()

    # ── 排程 ────────────────────────────────────────────

    def schedule(
        self,
        *,
        tg_user_id: int,
        scope: str,
        user_text: str,
        assistant_text: str,
    ) -> None:
        """背景抽取。立刻返回，不阻塞回覆。"""
        if not self._cfg.auto_memory or not _worth_extracting(user_text):
            return

        task = asyncio.create_task(
            self._run(tg_user_id, scope, user_text, assistant_text),
            name=f"memory:{tg_user_id}:{scope}",
        )
        # 保留參考，否則 task 可能被垃圾回收
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    # ── 實作 ────────────────────────────────────────────

    async def _run(
        self,
        tg_user_id: int,
        scope: str,
        user_text: str,
        assistant_text: str,
    ) -> None:
        try:
            existing = await self._sessions.notes(tg_user_id, scope)
            prompt = _PROMPT.format(
                existing="\n".join(f"- {note}" for note in existing) or "（目前沒有）",
                exchange=(
                    f"使用者：{truncate(user_text, 1200)}\n"
                    f"助理：{truncate(assistant_text, 800)}"
                ),
            )
            raw = await self._llm.summarise(prompt, max_tokens=300)
            facts = _parse(raw, existing)

            written = 0
            for fact in facts:
                if await self._sessions.add_note(
                    tg_user_id, fact, scope=scope, source="auto"
                ):
                    written += 1
            if written:
                logger.info("自動記憶（%s）新增 %d 則", scope, written)
        except Exception:
            # 記憶抽取失敗不該影響對話，記錄就好
            logger.exception("自動記憶失敗")


def _worth_extracting(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 12:
        return False
    # 只有媒體標註（〔貼圖，emoji：😭〕）時沒有抽取的意義
    if stripped.startswith("〔") and stripped.endswith("〕"):
        return False
    return True


def _parse(raw: str, existing: list[str]) -> list[str]:
    facts: list[str] = []
    known = {note.strip() for note in existing}

    for line in raw.splitlines():
        line = _MARKER.sub("", line.strip()).strip()
        if not line or _NOTHING.match(line):
            continue
        if line in known or line in facts:
            continue
        if len(line) > 200:
            continue
        facts.append(line)
        if len(facts) >= 3:
            break

    return facts
