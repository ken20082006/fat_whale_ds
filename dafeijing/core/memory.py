"""自動長期記憶。

每輪對話後背景跑一次抽取，從這一來一往裡找出「值得長期記住的事實」。
累積到一定數量後再跑一次整理，把零碎的筆記合併壓縮。

群組要點：事實要歸給**正確的人**，不是歸給發言的那個人。
甲說「@乙 你個 project 點」，學到的是關於乙的事，該記在乙頭上。
所以群組模式會要求模型為每一則標明是關於誰，再對應回 user id。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from .chain import normalise_name
from .session import SessionManager
from .util import truncate

logger = logging.getLogger(__name__)

# 條列符號與「1. 」這類編號。只砍這些，不要連事實開頭的數字一起砍掉。
_MARKER = re.compile(r"^\s*(?:[-•*]|\d+[.)])\s+")
_NOTHING = re.compile(r"^[（(]?\s*[無无]\s*[）)]?[。.]?$")
# 「名字｜事實」或「名字|事實」
# 歸屬用編號，不用名字。
#
# 曾經用顯示名稱當鍵，結果壞掉：某人的名字是
# 「🌼🙌🏻👋🏻👋🏻🐬🇭🇰（人可以無心，菜無惢會點...」，超長、全形、混大量 emoji，
# 模型無法可靠複述，於是退而求其次挑了名單上第一個名字 —— 事實就記到錯的人頭上。
# 編號沒有這個問題，模型不會把「3」寫成別的東西。
_BY_INDEX = re.compile(r"^\s*(\d{1,3})\s*[|｜]\s*(.+?)\s*$")
# 舊格式的退路：模型若還是寫了名字，仍然嘗試比對
_BY_NAME = re.compile(r"^\s*([^|｜]{1,60})\s*[|｜]\s*(.+?)\s*$")


@dataclass(frozen=True)
class Person:
    """對話中出現的人。名字取自引用串的實際發言者。"""

    user_id: int
    name: str


_EXTRACT_PRIVATE = """\
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
- **只是把自己的 ID、帳號名、顯示名抄成事實。** 這些名冊本來就有，
  抄進筆記只是每一輪多背一句。**除非對方明確要求你記住。**

**每則用一句話講完，寧可概括不要細節。** 這份筆記每次對話都會被讀取，
寫得越冗長越浪費。

既有筆記：
{existing}

對話：
{exchange}

若沒有新的、值得長期記住的事實，只回覆「無」。
否則每行一則事實，最多三則。直接寫事實，不要編號、不要前言、不要解釋。
"""

_EXTRACT_GROUP = """\
你在維護一份群組成員的長期筆記，這些筆記會在之後的對話中提供給助理參考。

從下面這段對話裡，找出值得長期記住的事實，**並標明每一則是關於誰**。

對話中出現的人（**只能用編號標明，不要寫名字**）：
{people}

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
- 關於不在名單上的人的事
- **對話中查到的通用知識或結論。** 例如「某鏡頭沒有出某接環」是相機常識，
  不是任何人的個人資料。寫進去只會變成噪音 —— 筆記要記的是「這個人本身」
- **只是把名冊資料抄成事實：ID、帳號名、顯示名。** 例如對方講「我個 ID 係
  101322959」，或你把發言者的顯示名寫成「使用者叫 X」—— 名冊本來就有這些，
  抄進筆記只是每一輪多背一句。「@某人叫某個稱呼」也一樣，**除非對方明確
  要求你記住**（那屬於上面「明確要求記住的事」）。

**每則用一句話講完，寧可概括不要細節。**

既有筆記：
{existing}

對話：
{exchange}

若沒有新的、值得長期記住的事實，只回覆「無」。
否則每行一則，格式固定為「編號｜事實」，編號必須取自上面那份名單。
例如：`2｜正在學 Rust`

最多三則。不要寫名字、不要前言、不要解釋。
"""

_CONSOLIDATE = """\
你在整理一份關於某個人的長期筆記。這份筆記會在之後的對話中提供給助理參考，
所以越精簡越好 —— 每一則都會佔用每一次對話的成本。

把下面的筆記合併、去重、壓縮，目標是最多 {target} 則。

規則：
- 只留下「會影響之後怎麼跟他相處」的事：身分、長期偏好、持續進行的事、明確要求記住的
- 合併同類：把零碎的細節收攏成一句概括
- 丟掉已過期、一次性的、或太細碎的內容
- 每則一句話講完，不要細節、不要舉例
- 直接輸出條列，不要前言、不要說明你做了什麼

現有筆記（共 {count} 則）：
{notes}
"""


_MEMORY_GATE_INSTRUCTIONS = (
    "判斷這段對話裡有沒有關於這位使用者本人、值得長期記住的事實。"
    "值得記的：身分與背景、長期偏好、持續進行的事、明確要求記住的事。"
    "不值得記的：一次性的問題、當下情緒、閒聊、問答本身、助理說過的話。"
)


class MemoryExtractor:
    def __init__(self, cfg, sessions: SessionManager, llm, decisions) -> None:
        self._cfg = cfg
        self._sessions = sessions
        self._llm = llm
        self._decisions = decisions
        self._tasks: set[asyncio.Task] = set()

    # ── 排程 ────────────────────────────────────────────

    def schedule(
        self,
        *,
        tg_user_id: int,
        scope: str,
        user_text: str,
        assistant_text: str,
        people: list[Person] | None = None,
    ) -> None:
        """背景抽取。立刻返回，不阻塞回覆。

        people 有值代表是群組：事實會依名字歸給正確的人。
        沒有的話是私聊，全部歸給對話的那一個人。
        """
        if not self._cfg.auto_memory or not _worth_extracting(user_text):
            return

        task = asyncio.create_task(
            self._run(tg_user_id, scope, user_text, assistant_text, people),
            name=f"memory:{tg_user_id}:{scope}",
        )
        # 保留參考，否則 task 可能被垃圾回收
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _worth_remembering(self, user_text: str, assistant_text: str) -> bool:
        """值不值得花一次抽取呼叫 —— 先問 Jev。

        抽取本來就在背景跑，所以這個閘省的不是延遲，而是那次 utility 模型呼叫。

        **判斷不可用時一律回 True。** 漏記一則正確的事實，比多花一次便宜呼叫
        嚴重得多 —— 這個閘只可以在有把握時才收窄，所以門檻刻意設得低。
        """
        if not self._cfg.decision_enabled or not self._cfg.auto_memory:
            return True

        decision = await self._decisions.noul(
            f"使用者說：{user_text}\n助理回：{assistant_text}",
            _MEMORY_GATE_INSTRUCTIONS,
        )
        if decision is None:
            return True

        keep = decision.value >= self._cfg.memory_decision_threshold
        logger.debug("記憶閘：%.2f → %s", decision.value, "記" if keep else "跳過")
        return keep

    # ── 抽取 ────────────────────────────────────────────

    async def _run(
        self,
        tg_user_id: int,
        scope: str,
        user_text: str,
        assistant_text: str,
        people: list[Person] | None,
    ) -> None:
        try:
            if not await self._worth_remembering(user_text, assistant_text):
                logger.debug("判斷不值得記，跳過抽取（%s）", scope)
                return

            if people:
                facts = await self._extract_group(scope, user_text, assistant_text, people)
            else:
                facts = await self._extract_private(tg_user_id, scope, user_text, assistant_text)

            written: dict[int, int] = {}
            for user_id, fact in facts:
                if await self._sessions.add_note(
                    user_id, fact, scope=scope, source="auto"
                ):
                    written[user_id] = written.get(user_id, 0) + 1

            for user_id, count in written.items():
                logger.info("自動記憶（%s）user=%s 新增 %d 則", scope, user_id, count)

            # 整理只針對剛剛有寫入的人，不必掃全部
            for user_id in written:
                await self._consolidate(user_id, scope)
        except Exception:
            # 記憶抽取失敗不該影響對話，記錄就好
            logger.exception("自動記憶失敗")

    async def _extract_private(
        self, tg_user_id: int, scope: str, user_text: str, assistant_text: str
    ) -> list[tuple[int, str]]:
        existing = await self._sessions.notes(tg_user_id, scope)
        prompt = _EXTRACT_PRIVATE.format(
            existing="\n".join(f"- {note}" for note in existing) or "（目前沒有）",
            exchange=(
                f"使用者：{truncate(user_text, 1200)}\n"
                f"助理：{truncate(assistant_text, 800)}"
            ),
        )
        raw = await self._llm.summarise(prompt, max_tokens=300)
        return [(tg_user_id, fact) for fact in _parse(raw, existing, limit=3)]

    async def _extract_group(
        self,
        scope: str,
        user_text: str,
        assistant_text: str,
        people: list[Person],
    ) -> list[tuple[int, str]]:
        """群組模式：每一則事實都要標明是關於誰。用編號歸屬，不用名字。"""
        indexed = list(enumerate(people, start=1))
        by_index = {index: person.user_id for index, person in indexed}
        by_name = {normalise_name(person.name): person.user_id for person in people}

        existing = await self._sessions.notes_for(
            [person.user_id for person in people], scope
        )

        # 既有筆記依人分行，模型才比對得出哪些是新事實
        name_of = {person.user_id: person.name for person in people}
        existing_lines: list[str] = []
        for user_id, notes in existing.items():
            for note in notes:
                existing_lines.append(f"{name_of.get(user_id, user_id)}｜{note}")

        prompt = _EXTRACT_GROUP.format(
            people="\n".join(f"{index}｜{person.name}" for index, person in indexed),
            existing="\n".join(f"- {line}" for line in existing_lines) or "（目前沒有）",
            exchange=(
                f"對話內容：\n{truncate(user_text, 1500)}\n\n"
                f"助理的回覆：{truncate(assistant_text, 600)}"
            ),
        )
        raw = await self._llm.summarise(prompt, max_tokens=400)
        return _parse_attributed(raw, by_index, by_name, existing)

    # ── 定期整理 ────────────────────────────────────────

    async def _consolidate(self, tg_user_id: int, scope: str) -> None:
        """筆記累積到一定數量就合併壓縮。沒有上限的累積會讓每次對話都變貴。"""
        threshold = self._cfg.notes_consolidate_threshold
        if threshold <= 0:
            return

        # 這裡要拿全部，不能受顯示用的上限影響
        notes = await self._sessions.notes(tg_user_id, scope, limit=1000)
        if len(notes) < threshold:
            return

        target = self._cfg.notes_consolidate_target
        prompt = _CONSOLIDATE.format(
            target=target,
            count=len(notes),
            notes="\n".join(f"- {note}" for note in notes),
        )

        try:
            raw = await self._llm.summarise(prompt, max_tokens=800)
            merged = _parse(raw, existing=[], limit=target)
        except Exception:
            logger.exception("整理筆記失敗，保留原樣")
            return

        if not merged:
            logger.warning("整理筆記得到空結果，保留原本的 %d 則", len(notes))
            return

        await self._sessions.replace_notes(tg_user_id, scope, merged)
        logger.info("整理筆記（%s）：%d 則壓縮為 %d 則", scope, len(notes), len(merged))


def _parse_attributed(
    raw: str,
    by_index: dict[int, int],
    by_name: dict[str, int],
    existing: dict[int, list[str]],
    limit: int = 3,
) -> list[tuple[int, str]]:
    """解析「編號｜事實」，編號對不上就整行丟掉。

    優先認編號；模型若仍寫了名字，才退回用名字比對。

    寧可漏記，也不要把關於甲的事記到乙頭上 —— 記錯比沒記更糟，
    因為之後會拿錯誤的記憶去回應，而使用者會以為它真的記得。
    """
    facts: list[tuple[int, str]] = []

    for line in raw.splitlines():
        cleaned = _MARKER.sub("", line.strip()).strip()
        if not cleaned or _NOTHING.match(cleaned):
            continue

        user_id: int | None = None
        fact = ""

        numbered = _BY_INDEX.match(cleaned)
        if numbered is not None:
            user_id = by_index.get(int(numbered.group(1)))
            fact = numbered.group(2).strip()
            if user_id is None:
                logger.debug("群組抽取：編號不在名單上，略過：%s", cleaned[:40])
                continue
        else:
            named = _BY_NAME.match(cleaned)
            if named is None:
                logger.debug("群組抽取：忽略無法歸屬的行：%s", cleaned[:40])
                continue
            user_id = by_name.get(normalise_name(named.group(1)))
            fact = named.group(2).strip()
            if user_id is None:
                # 名字太長、含 emoji、或模型複述得不精確時會落到這裡。
                # 這正是編號存在的理由。
                logger.debug("群組抽取：名字對不上，略過：%s", named.group(1)[:30])
                continue

        if not fact or len(fact) > 200:
            continue
        if fact in existing.get(user_id, []):
            continue
        if (user_id, fact) in facts:
            continue

        facts.append((user_id, fact))
        if len(facts) >= limit:
            break

    return facts


def _worth_extracting(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 12:
        return False
    # 只有媒體標註（〔貼圖〕）時沒有抽取的意義
    if stripped.startswith("〔") and stripped.endswith("〕"):
        return False
    return True


def _parse(raw: str, existing: list[str], limit: int = 3) -> list[str]:
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
        if len(facts) >= limit:
            break

    return facts
