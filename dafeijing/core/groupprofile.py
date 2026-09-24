"""群組概況。

每個群一則「這個群體本身」的樣貌 —— 主題、氣氛、慣例、近期話題。

與 `memory_notes` 的分別：那些是「關於某個人」的事實，每一則都屬於一個
`user_id`；概況是「關於這個群」的，不屬於任何人。兩者要的東西不同，所以
分開存，而不是把 `memory_notes.user_id` 改成可為 NULL。

素材限於機器人**親自參與過**的交流（被 @ 或被回覆的那些）。它在群組裡
旁觀到的其他閒聊不會進來 —— 那些只在 `group_cache` 留 72 小時，不該被
沉澱成永久記錄。

概況是一段文字而不是一條條筆記，所以天生有界，不需要濃縮機制。刻意寫得
短：「大概」比「一字不漏」更耐用，也更便宜。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .session import SessionManager
from .util import now_iso, truncate

logger = logging.getLogger(__name__)

_ROLE_LABEL = {"user": "對方", "assistant": "你"}

_SUMMARY_PROMPT = """\
你在維護一份「這個群組」的概況。它不是關於某個人的檔案，而是這個群體本身的樣貌。

你在這個群組裡參與過的對話（由舊到新）：
<對話>
{exchanges}
</對話>

現有的概況：
<現況>
{existing}
</現況>

把新內容併入，寫成一段簡短的概況，涵蓋：
- 這個群在討論什麼、成員關心什麼
- 群體的氣氛與互動方式（認真程度、玩笑尺度、用語習慣）
- 群組層面的規矩或慣例
- 最近的話題走向

規則：
- **只要大意，不要逐字記錄。** 三到六句就夠。
- 不要寫特定個人的私事 —— 那些屬於個人筆記，不是群組概況。
- 不要寫一次性的事，也不要寫你自己說過的話。
- 現況裡仍然成立的就保留，過時的就換掉，不要只顧著加。
- 沒有值得留下的新內容時，原樣輸出既有概況。

只輸出概況本身，不要前言、不要標題、不要引號。
"""


class GroupProfiler:
    def __init__(self, cfg, sessions: SessionManager, llm) -> None:
        self._cfg = cfg
        self._sessions = sessions
        self._llm = llm
        self._tasks: set[asyncio.Task] = set()
        # 同一個群不要同時跑兩次 —— 兩邊都會讀到舊概況然後互相覆蓋
        self._running: set[int] = set()
        self.updates = 0

    # ── 排程 ────────────────────────────────────────────

    def schedule(self, chat_id: int) -> None:
        """背景更新。立刻返回，不阻塞回覆。"""
        if not self._cfg.group_profile_enabled or chat_id in self._running:
            return
        task = asyncio.create_task(self._run(chat_id), name=f"profile:{chat_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    # ── 產生 ────────────────────────────────────────────

    async def _run(self, chat_id: int) -> None:
        self._running.add(chat_id)
        try:
            profile = await self._sessions.get_group_profile(chat_id)
            if not self._due(profile):
                return

            since = profile["summarized_at"] if profile else None
            exchanges = await self._sessions.group_exchanges_since(
                chat_id, since, limit=self._cfg.group_profile_max_exchanges
            )
            if len(exchanges) < self._cfg.group_profile_min_exchanges:
                return

            # 先記下涵蓋範圍才開始跑。摘要期間進來的新訊息留到下一輪，
            # 否則跑完把時間點往前推，那幾則就永遠漏了。
            cutoff = now_iso()

            body = "\n".join(
                f"{_ROLE_LABEL.get(row['role'], row['role'])}："
                f"{truncate(row['content'] or '', 400)}"
                for row in exchanges
            )
            prompt = _SUMMARY_PROMPT.format(
                exchanges=body,
                existing=(profile["content"] if profile else "（還沒有）"),
            )
            summary = (await self._llm.summarise(prompt, max_tokens=500)).strip()
            if not summary:
                return

            await self._sessions.set_group_profile(
                chat_id,
                truncate(summary, self._cfg.group_profile_max_chars),
                cutoff,
            )
            self.updates += 1
            logger.info(
                "群組概況更新（%s）：%d 則交流 → %d 字",
                chat_id,
                len(exchanges),
                len(summary),
            )
        except Exception:
            # 概況失敗不該影響對話
            logger.exception("群組概況更新失敗")
        finally:
            self._running.discard(chat_id)

    def _due(self, profile: dict | None) -> bool:
        """距離上次更新夠久了嗎。

        概況是慢慢形成的。每幾則就重寫一次只會讓它跳來跳去、每次都多付一次
        呼叫，而群組的樣貌本來就不是幾句話就會變。
        """
        if profile is None or self._cfg.group_profile_min_hours <= 0:
            return True
        try:
            last = datetime.strptime(
                profile["summarized_at"], "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return True
        age = (datetime.now(timezone.utc) - last).total_seconds()
        return age >= self._cfg.group_profile_min_hours * 3600
