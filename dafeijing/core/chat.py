"""對話的完整流程。

私聊與群組共用同一條管線，差別只在於：群組帶入引用串、回覆上限較短、且不讀長期記憶。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..llm.decisions import (
    Assessment,
    DecisionsClient,
    budget_factor,
    resolve_reasoning,
    route_flags,
    tone_vibe,
)
from ..llm.openrouter import LLMResult, OpenRouterClient
from .access import AccessControl
from .media import PreparedImage
from .memory import MemoryExtractor, Person
from .persona import Persona, PersonaContext
from .security import LEAK_REPLY, find_system_leak
from .session import SessionManager, SessionRef, scope_for
from .stickers import StickerLibrary, extract_marker
from .util import today_text
from .webfetch import (
    extract_search_marker,
    fetch_all,
    find_urls,
    strip_citations,
    wants_search,
)
from .usage import UsageLog

logger = logging.getLogger(__name__)

# 一次最多帶幾個人的筆記進提示。帶太多會讓 system prompt 暴漲，
# 而且「一次問三個人的事」本來就少見。
MAX_MENTIONED_NOTES = 3

# /vibe 的等級順序。tone_vibe() 只會沿這個順序往下調，不會往上推 ——
# 使用者的選擇是上限，逐則判斷只可以把演出收窄。
VIBE_ORDER = ("low", "mid", "high")


def _fmt_score(value: float | None) -> str:
    """日誌用。沒判斷到就寫 —，不要寫 None。"""
    return "—" if value is None else f"{value:.2f}"


@dataclass
class ChatRequest:
    tg_user_id: int
    display_name: str | None
    text: str
    chat_id: int
    session: SessionRef
    is_group: bool = False
    chain_text: str | None = None
    chain_messages: list[dict] | None = None
    images: list[PreparedImage] = field(default_factory=list)
    force_search: bool = False
    # 群組限定：這一串裡出現過的人。抽取時用來把事實歸給正確的人。
    people: list[Person] | None = None
    # 這則訊息 @ 到的人。他們在同一場合的筆記會被帶進提示，好回答「乙怎樣怎樣」。
    mentioned: list[Person] | None = None


@dataclass
class ChatOutcome:
    """一輪回覆的結果。貼圖與文字分開，因為送出方式不同。"""

    text: str
    result: LLMResult
    sticker_file_id: str | None = None


class ChatService:
    def __init__(
        self,
        cfg,
        persona: Persona,
        sessions: SessionManager,
        access: AccessControl,
        llm: OpenRouterClient,
        usage: UsageLog,
        memory: MemoryExtractor,
        stickers: StickerLibrary,
        decisions: DecisionsClient,
        profiler,
    ) -> None:
        self._cfg = cfg
        self._persona = persona
        self._sessions = sessions
        self._access = access
        self._llm = llm
        self._usage = usage
        self._memory = memory
        self._stickers = stickers
        self._decisions = decisions
        self._profiler = profiler
        self.blocked_leaks = 0
        self.web_searches = 0
        self.fetched_pages = 0

    async def _assess(self, text: str) -> Assessment | None:
        """一次問齊：怎麼處理、要幾深、幾放開。

        只餵「對方自己這一句」。群組的引用串是別人講的話，拿它來判斷
        等於替整個群組查、替別人講過的話查。
        """
        if not text.strip():
            return None
        return await self._decisions.assess(text)

    def _pick_reasoning(self, user, route: str | None) -> bool:
        """個人指定 > 路由判斷 > 全域後備。"""
        personal = user["reasoning"] if user is not None else None
        indicated, _search = route_flags(route)
        return resolve_reasoning(
            personal, indicated, fallback=self._cfg.reasoning_enabled
        )

    def _pick_search(self, req: ChatRequest, route: str | None) -> bool:
        """搜尋：模式決定誰說了算。

        `off` 完全不搜；`always` 與 `/search`（force_search）是明確要求，
        不受判斷影響；其餘（`auto` / `trigger`）交給判斷。

        regex 在這裡只可以做**後備**、不可以做否決 —— 判斷失手時仍然捉得到
        明講的「上網查」。反過來讓 regex 能否決判斷，就會退化回舊行為。
        """
        mode = self._cfg.search_mode
        if mode == "off":
            return False
        if req.force_search or mode == "always":
            return True
        _reason, by_route = route_flags(route)
        return bool(by_route) or wants_search(req.text or "", mode)

    async def respond(self, req: ChatRequest) -> ChatOutcome:
        user = await self._access.get_user(req.tg_user_id)
        vibe = (user["vibe"] if user else None) or "mid"

        # 判斷只看「對方自己這一句」。
        #
        # 群組的 base_text 是整條引用串，若拿它來判斷，串裡任何一個人提到
        # 「最新」或「版本」都會觸發 —— 等於替整個群組查，而且是替別人講過
        # 的話查。判斷依據必須是當下這一句。
        trigger_text = req.text or ""

        # 一個呼叫同時問齊三件事，取代舊的 regex 搜尋閘與「讓主模型自己
        # 決定」的標記重跑。那兩個閘為什麼失效，見 TODO.md。
        assessment = await self._assess(trigger_text)
        route = assessment.route if assessment else None

        reasoning = self._pick_reasoning(user, route)
        search = self._pick_search(req, route)

        # 逐則把演出收窄（只降不升）。人設原本就寫「對方情緒低落時收起
        # 角色扮演」，這裡把那條規則做成明確信號，不必靠主模型自己察覺。
        if assessment and assessment.tone is not None:
            vibe = tone_vibe(assessment.tone, vibe, VIBE_ORDER)

        if assessment:
            logger.info(
                "判斷 route=%s depth=%s tone=%s → 推理%s、搜尋%s（%s）",
                route,
                _fmt_score(assessment.depth),
                _fmt_score(assessment.tone),
                "開" if reasoning else "關",
                "開" if search else "關",
                trigger_text[:20].replace("\n", " "),
            )
        if search:
            self.web_searches += 1

        # 長期筆記依場合分開：私聊獨立，每個群組也各自獨立。見 scope_for 的說明。
        scope = scope_for(req.is_group, req.chat_id)
        notes = await self._sessions.notes(req.tg_user_id, scope)

        # 被 @ 到的人，他們在同一場合的筆記也要帶上 —— 否則甲問「乙在做什麼」
        # 時，助理手上只有甲的筆記，答不出來。這些筆記與甲自己的同屬一個場合，
        # 可見範圍一樣，沒有額外揭露。
        others_notes: list[tuple[str, list[str]]] = []
        for person in (req.mentioned or [])[:MAX_MENTIONED_NOTES]:
            if person.user_id == req.tg_user_id:
                continue
            other = await self._sessions.notes(person.user_id, scope)
            if other:
                others_notes.append((person.name, other))

        # 群組概況：這個群體本身的樣貌。與筆記不同，它不屬於任何一個人，
        # 所以不分發言者是誰都會載入。見 core/groupprofile.py。
        group_profile = None
        if req.is_group:
            row = await self._sessions.get_group_profile(req.chat_id)
            group_profile = row["content"] if row else None

        system_prompt = self._persona.build(
            PersonaContext(
                vibe=vibe,
                display_name=req.display_name,
                notes=notes,
                summary=req.session.summary,
                is_group=req.is_group,
                sticker_menu=self._stickers.menu() if self._stickers.available else None,
                today=today_text(
                    self._cfg.timezone_offset_hours, self._cfg.timezone_label
                ),
                others_notes=others_notes,
                group_profile=group_profile,
                can_search=self._cfg.search_mode != "off",
                can_fetch=self._cfg.fetch_max_urls > 0,
                # 只有「這一則還沒有搜尋」時才告訴模型它可以自己要求搜尋。
                # 否則它會照著那段指示「只回覆 [[搜尋:...]]，不要寫其他內容」，
                # 而搜尋其實已經做過了 —— 標記被清掉之後整則回覆變成空白。
                self_search=(
                    not search and self._cfg.search_mode not in ("off", "always")
                ),
            )
        )

        base_text = req.chain_text or req.text

        # 連結連同被引用的那一則一起看 ——「引用一條連結再 @ 它」是常見用法。
        url_source = trigger_text
        if req.chain_messages and len(req.chain_messages) >= 2:
            parent = req.chain_messages[-2].get("text") or ""
            url_source = f"{trigger_text}\n{parent}"

        # 抓回來的內容是外部文字，包成資料區塊送出去，但不落庫 ——
        # 存進對話歷史會讓每一輪都背著整頁網頁，成本會失控。
        page_blocks: list[str] = []
        if self._cfg.fetch_max_urls > 0:
            pages = await fetch_all(find_urls(url_source), limit=self._cfg.fetch_max_urls)
            if pages:
                page_blocks = [page.as_block() for page in pages]
                self.fetched_pages += len(pages)

        mode = self._cfg.search_mode

        # 送出去的用整條引用串；存進歷史的只用「對方自己那一句」。
        #
        # 群組的引用串是累積的 —— 同一條串每輪都存一次，歷史會重複膨脹
        # （實測六輪就從 2.1k 漲到 3.0k token），而那些內容在下一輪的
        # 引用串裡又會再出現一次。歷史只需要記「誰在什麼時候問了什麼」。
        stored_base = trigger_text if req.is_group and trigger_text else base_text

        if page_blocks:
            user_content = f"{base_text}\n\n" + "\n\n".join(page_blocks)
            stored_content = f"{stored_base}\n\n〔讀取了 {len(page_blocks)} 個連結〕"
        else:
            user_content = base_text
            stored_content = stored_base

        history = await self._sessions.window(req.session.id)

        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        messages.extend({"role": item["role"], "content": item["content"]} for item in history)

        # 有圖片時改用多模態格式。歷史訊息一律維持純文字 ——
        # 圖片不落庫（見下方 append），所以舊訊息本來也沒有圖可放。
        if req.images:
            content: list[dict] = [{"type": "text", "text": user_content}]
            content.extend(
                {"type": "image_url", "image_url": {"url": image.data_url}}
                for image in req.images
            )
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": user_content})

        max_tokens = (
            self._cfg.group_reply_max_tokens if req.is_group else self._cfg.private_reply_max_tokens
        )
        if reasoning:
            # 推理 token 會**吃掉**這個額度，用完 content 會變 null（使用者收到
            # 「本鯨想得太久，額度用完了」）。判斷說要思考得越深，就多留一點。
            max_tokens = int(
                max_tokens
                * budget_factor(
                    assessment.depth if assessment else None,
                    self._cfg.reasoning_budget_low,
                    self._cfg.reasoning_budget_high,
                )
            )

        result = await self._llm.chat(
            messages, max_tokens=max_tokens, reasoning=reasoning, web_search=search
        )

        # 模型自己要求搜尋：它只回了一行標記時，帶著外掛重跑一次。
        # 這是唯一能讓模型自決的方法 —— web 外掛沒有「讓模型啟用自己」的介面。
        #
        # 這裡**不是**「只有在搜尋還沒開時才處理」。模型即使已經拿到搜尋結果，
        # 仍可能再要求查一次（persona 有教它這個標記）。而那段指示寫明
        # 「只回覆這一行，不要寫其他內容」—— 標記往往就是它的全部輸出，
        # 清掉之後變成空白，使用者只收到一句沒頭沒腦的錯誤訊息。
        # 真實案例：問「今日恆指幾多」，判斷已開搜尋，模型仍只回了標記，
        # 輸出 24 個 token，清完是空的。
        stripped, query = extract_search_marker(result.text)
        if query and mode != "off" and (not search or not stripped.strip()):
            logger.info("模型要求搜尋：%s", query)
            self.web_searches += 1
            search = True
            result = await self._llm.chat(
                [
                    *messages,
                    {
                        "role": "user",
                        "content": (
                            f"（先上網查「{query}」，再用查到的內容回答我上一則問題。"
                            f"這次不要再輸出標記。）"
                        ),
                    },
                ],
                max_tokens=max_tokens,
                reasoning=reasoning,
                web_search=True,
            )

        # 先把標記拿掉 —— 那是給系統看的，不能留在訊息裡。
        # 模型有可能在第二次仍然輸出搜尋標記，所以兩種都清。
        result.text, _ = extract_search_marker(result.text)

        # 清掉來源標註。那是搜尋外掛的預設行為（要模型標出處），
        # 提示裡雖然已經叫它不要標，模型不一定每次都聽，所以在輸出端再清一次。
        result.text = strip_citations(result.text)

        if not result.text:
            # 走到這裡還是空白。最常見的原因是模型只輸出了一個搜尋標記，
            # 清掉之後就沒了 —— 那句話原本寫「查完不知道該說什麼」，但它
            # 其實什麼都沒說，使用者只會一頭霧水。講明白，也留下痕跡好追。
            logger.warning(
                "模型回覆清理後是空白（finish_reason=%s、輸出 %d token）",
                result.finish_reason,
                result.completion_tokens,
            )
            result.text = "本鯨這次卡住了，再問一次好嗎。"
        cleaned, sticker_index = extract_marker(result.text)
        result.text = cleaned

        sticker_file_id: str | None = None
        if sticker_index is not None:
            entry = self._stickers.resolve(sticker_index)
            if entry is not None:
                sticker_file_id = entry.file_id
            else:
                # 模型編了清單上沒有的號碼。標記已經拿掉，就當作沒這回事，
                # 不要送出不相干的貼圖。
                logger.warning("模型指定的貼圖編號不存在：%s", sticker_index)

        # 輸出側的洩漏檢查。在落庫之前替換掉，被攔下的內容才不會進到對話歷史裡，
        # 免得下一輪又被當成自己說過的話而強化。
        leak = find_system_leak(result.text, self._persona.static_text)
        if leak:
            self.blocked_leaks += 1
            logger.warning(
                "攔下疑似系統提示洩漏（%d 字重疊）：%s…", len(leak), leak[:40]
            )
            result.text = LEAK_REPLY

        # 成功後才落庫。失敗的回合不留下痕跡，使用者重試時不會出現半截對話。
        # 只留文字描述不留圖檔：省空間，也避免使用者的照片被長期保存。
        await self._sessions.append(
            req.session.id, "user", stored_content, has_image=bool(req.images)
        )
        await self._sessions.append(req.session.id, "assistant", result.text)

        await self._usage.record(
            user_id=req.tg_user_id,
            chat_id=req.chat_id,
            model=result.model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cached_tokens=result.cached_tokens,
            reasoning_tokens=result.reasoning_tokens,
            image_tokens=result.image_tokens,
            cost=result.cost,
        )

        try:
            await self._sessions.maybe_compact(req.session.id, self._llm.summarise)
        except Exception:
            logger.exception("壓縮歷史失敗，不影響本次回覆")

        # 背景抽取長期記憶。用 req.text 而非 user_content ——
        # 群組的 user_content 是整條引用串，含其他人的發言，不該算在這個人頭上。
        self._memory.schedule(
            tg_user_id=req.tg_user_id,
            scope=scope,
            user_text=req.text or base_text,
            assistant_text=result.text,
            people=req.people,
        )

        # 群組概況。累積夠多新的交流、而且距離上次夠久才會真的跑 ——
        # 判斷在 profiler 裡面，這裡只是舉手。見 core/groupprofile.py。
        if req.is_group:
            self._profiler.schedule(req.chat_id)

        logger.info(
            "%s → %d in / %d out（推理 %d、快取 %d）%s%s",
            "群組" if req.is_group else "私聊",
            result.prompt_tokens,
            result.completion_tokens,
            result.reasoning_tokens,
            result.cached_tokens,
            "｜聯網搜尋" if search else "",
            f"｜讀取 {len(page_blocks)} 個連結" if page_blocks else "",
        )
        return ChatOutcome(
            text=result.text,
            result=result,
            sticker_file_id=sticker_file_id,
        )
