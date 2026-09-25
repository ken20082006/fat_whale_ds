"""對話的完整流程。

私聊與群組共用同一條管線，差別只在於：群組帶入引用串、回覆上限較短、且不讀長期記憶。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..llm.decisions import (
    EFFORT_THOROUGH,
    Assessment,
    DecisionsClient,
    budget_factor,
    resolve_reasoning,
    route_flags,
    search_results,
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
from .util import today_text, truncate
from .webfetch import (
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


def pick_search(
    *,
    mode: str,
    force: bool,
    text: str,
    route: str | None,
    effort: str | None,
) -> str:
    """這一則要怎麼搜。回傳 "off" / "tool" / "force"。

    - `off`   完全不搜
    - `tool`  帶伺服器端工具，由模型自己決定搜幾次、搜什麼
    - `force` 走外掛強制搜一次 —— 伺服器端工具沒有 tool_choice、官方也沒
              記載可強制，所以需要「保證會搜」的場合只能走外掛。

    抽成純函式是因為這段已經出錯兩次，值得有測試釘住。
    """
    # /search 是明確要求，即使模式是 off 也應該生效。舊版寫成
    # `force and mode != "off"`，於是 off 之下 /search 會靜默不查，
    # 使用者無從得知。
    if force:
        return "force"
    if mode == "off":
        return "off"
    if mode == "always":
        return "force"
    if mode == "trigger":
        # 這個模式刻意只認明講的，所以不看判斷結果。
        return "tool" if wants_search(text, mode) else "off"

    # auto。**判斷說「小眾冷門、撈少會漏」，本身就意味著應該去搜。**
    #
    # route 與 effort 是分開問的，會出現自相矛盾的組合。實際案例：問
    # 「iphone 18幾時出」之後追問「duo呢」，route 說 direct、effort 卻說
    # thorough（它認得那個詞冷門）—— 結果不搜，助理就一路否認那個型號
    # 存在，而實際上搜一次就找到，連 apple.com 的新聞稿都有。
    if effort == EFFORT_THOROUGH:
        return "tool"

    # regex 只做**後備**、不做否決 —— 判斷失手時仍然捉得到明講的「上網查」。
    # 反過來讓 regex 能否決判斷，就會退化回舊行為。
    _reason, by_route = route_flags(route)
    if by_route or wants_search(text, mode):
        return "tool"
    return "off"


def _source_hosts(sources: list[str], limit: int = 3) -> list[str]:
    """把來源網址收成最多幾個網域，去重並保持順序。"""
    seen: set[str] = set()
    out: list[str] = []
    for url in sources or []:
        try:
            host = (urlparse(url).netloc or "").lower()
        except ValueError:
            continue
        host = host.removeprefix("www.")
        if host and host not in seen:
            seen.add(host)
            out.append(host)
        if len(out) >= limit:
            break
    return out


def _reply_with_sources(result) -> str:
    """落庫用的回覆。有查到東西就附一行來源註記。

    使用者看不到這一行（回覆已經送出去了），但**下一輪的模型看得到** ——
    它才答得出「你邊度睇到」，也不會因為忘了自己查過而重複查。

    用〔〕開頭與媒體標註一致，人設那邊有說明那是系統註記、不是它說過的話。
    """
    hosts = _source_hosts(result.sources)
    if not hosts:
        return result.text
    return f"{result.text}\n〔查了：{'、'.join(hosts)}〕"


# 助理搜過網時，落庫的回覆會帶這一行（見 _reply_with_sources）。
# 判斷用它認出「上一輪已經查過」。
_SEARCHED_MARK = "〔查了："


def build_state(text: str, recent: list[dict], limit: int = 300) -> str:
    """組成判斷要看的內容。

    **要帶上一輪對話。** 短追問單獨看會失真 ——「iphone duo喎」單獨看只是
    一個三個字的片段，判斷成「直接答」；帶上「上一句問緊開賣日期」之後
    才會判斷成「要搜尋」。實測：單獨 direct（信心 0.81）、帶上文 search。
    沒有上文的話，使用者追問一次就等於白問。

    **而且要看對方是不是在追問。** 判斷本來是逐則獨立的，所以「對方已經
    問過、你查過、佢仍然追問」這件事它看不出來 —— 實際事故：使用者連續
    六則強調同一件事，助理四次都判斷成唔使搜，甚至答「我冇開聯網搜尋」。
    上一輪查過而對方仍然追問，通常代表查得不夠，要再查或者想清楚一點。
    """
    if not recent:
        return text

    lines = [
        f"{'使用者' if item.get("role") == "user" else "助理"}："
        f"{truncate(item.get("content") or "", limit)}"
        for item in recent
    ]
    state = "上一輪對話：\n" + "\n".join(lines) + f"\n\n使用者現在說：{text}"

    if any(
        item.get("role") == "assistant" and _SEARCHED_MARK in (item.get("content") or "")
        for item in recent
    ):
        state += (
            "\n\n（注意：你上一輪已經上網查過，對方仍然追問 —— 通常代表查得"
            "不夠或者答得不好。這次應該再加強：再查一次（換個講法），"
            "或者想清楚一點才答。）"
        )
    return state


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
    # 這一則媒體的內容描述（影片／動圖外包回來的）。
    #
    # **刻意放在這裡而不是訊息文字裡。** 訊息會落庫，下一輪的歷史就會多
    # 一句形狀完全一樣的描述，模型分不清哪一句屬於眼前這條片 —— 實際
    # 表現是張冠李戴，再之後索性自己編（真實事故：描述寫住黑人小男孩，
    # 助理答「一隻貓蹲喺鍵盤上面」）。放進 system prompt 就只有這輪見到。
    media_note: str | None = None


@dataclass
class ChatOutcome:
    """一輪回覆的結果。貼圖與文字分開，因為送出方式不同。"""

    text: str
    result: LLMResult
    sticker_file_id: str | None = None
    # 思考過程（beta）。None 代表「唔使貼」—— 功能未開、這次沒有推理、
    # 或者內容疑似洩漏被擋下。呼叫端只負責「有就送」。
    reasoning: str | None = None


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

    async def _assess(self, text: str, session_id: int) -> Assessment | None:
        """一次問齊：怎麼處理、要幾深、幾放開、要撈幾多。

        只看「對方自己這一句」再加**上一輪**：群組的引用串是別人講的話，
        拿整條來判斷等於替整個群組查；但完全不帶上文的話，短追問會失真
        （見 build_state）。
        """
        if not text.strip():
            return None
        recent = await self._sessions.recent_messages(
            session_id, self._cfg.decision_context_messages
        )
        return await self._decisions.assess(build_state(text, recent))

    def _pick_reasoning(self, user, route: str | None) -> bool:
        """個人指定 > 路由判斷 > 全域後備。"""
        personal = user["reasoning"] if user is not None else None
        indicated, _search = route_flags(route)
        return resolve_reasoning(
            personal, indicated, fallback=self._cfg.reasoning_enabled
        )

    def _pick_search(
        self, req: ChatRequest, route: str | None, effort: str | None
    ) -> str:
        return pick_search(
            mode=self._cfg.search_mode,
            force=req.force_search,
            text=req.text or "",
            route=route,
            effort=effort,
        )

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
        assessment = await self._assess(trigger_text, req.session.id)
        route = assessment.route if assessment else None

        reasoning = self._pick_reasoning(user, route)
        search_mode = self._pick_search(
            req, route, assessment.search_effort if assessment else None
        )
        searching = search_mode != "off"

        # 要撈幾多條由判斷決定。外掛每次請求只搜一次、查詢由引擎自己從
        # 對話推導，這是唯一能調召回率的地方 —— 小眾名詞撈得少就會漏。
        wanted_results = search_results(
            assessment.search_effort if assessment else None,
            self._cfg.search_results_quick,
            self._cfg.search_max_results,
            self._cfg.search_results_thorough,
        )

        # 逐則把演出收窄（只降不升）。人設原本就寫「對方情緒低落時收起
        # 角色扮演」，這裡把那條規則做成明確信號，不必靠主模型自己察覺。
        if assessment and assessment.tone is not None:
            vibe = tone_vibe(assessment.tone, vibe, VIBE_ORDER)

        if assessment:
            logger.info(
                "判斷 route=%s depth=%s tone=%s effort=%s → 推理%s、搜尋%s（%s）",
                route,
                _fmt_score(assessment.depth),
                _fmt_score(assessment.tone),
                assessment.search_effort or "—",
                "開" if reasoning else "關",
                f"{search_mode}（{wanted_results} 條）" if searching else "關",
                trigger_text[:20].replace("\n", " "),
            )
        if searching:
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
                can_fetch=self._cfg.fetch_max_urls > 0,
                search_policy=search_mode,
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

        # 這一則的媒體內容：附在**送出去的**訊息後面，但**不落庫**。
        #
        # 試過兩個位置，都唔得：
        #   1. 寫進訊息文字 → 落庫 → 下一輪歷史又多一句形狀一樣的，模型
        #      分不清哪句屬於眼前這條片（張冠李戴，再之後索性自己編）。
        #   2. 放 system prompt → 離訊息太遠，模型會忽略，照樣答「睇唔到」。
        #
        # 附在訊息後面最靠近，而且歷史乾淨。抓回來的網頁本來就用這個做法。
        if req.media_note:
            user_content = (
                f"{user_content}\n\n"
                "[這一則的影片內容 —— 你看過了，就係以下咁多]\n"
                f"{req.media_note}\n"
                "[影片內容結束]"
            )
            stored_content = f"{stored_content}\n〔對方傳了一段影片〕"

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
            messages,
            max_tokens=max_tokens,
            reasoning=reasoning,
            search=search_mode == "tool",
            force_search=search_mode == "force",
            search_results=wanted_results,
        )

        # 清掉來源標註。外掛的預設行為會要模型標出處（格式像
        # `(mashable.com (https://...))`），提示裡雖然已經叫它不要標，
        # 模型不一定每次都聽，所以在輸出端再清一次。
        #
        # 伺服器端工具那條路不會產生這種標註 —— 它把來源放在
        # message.annotations 裡（見 llm/openrouter.py），正文是乾淨的。
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

        # 思考過程（beta）。開關關著時連讀都不讀。
        #
        # **一定要過同一道洩漏檢查。** 思考是模型自言自語的原文，它可能覆述
        # 到人設或系統提示 —— 而人設那部分正是我們不想讓人看見的。命中就
        # 整段不貼，不做部分遮蔽：半截的思考無意義，拼起來反而更容易看出原文。
        #
        # 它**不落庫** —— 落庫的話下一輪歷史會多一段，模型就會當成自己說過
        # 的話（與 media_note 同一個道理）。
        reasoning_text: str | None = None
        if self._cfg.show_reasoning and result.reasoning:
            reasoning_leak = find_system_leak(result.reasoning, self._persona.static_text)
            if reasoning_leak:
                logger.warning(
                    "思考過程疑似洩漏（%d 字重疊），不貼出：%s…",
                    len(reasoning_leak),
                    reasoning_leak[:40],
                )
            else:
                reasoning_text = truncate(
                    result.reasoning, self._cfg.show_reasoning_max_chars
                )

        # 成功後才落庫。失敗的回合不留下痕跡，使用者重試時不會出現半截對話。
        # 只留文字描述不留圖檔：省空間，也避免使用者的照片被長期保存。
        await self._sessions.append(
            req.session.id, "user", stored_content, has_image=bool(req.images)
        )
        await self._sessions.append(
            req.session.id, "assistant", _reply_with_sources(result)
        )

        await self._usage.record(
            user_id=req.tg_user_id,
            chat_id=req.chat_id,
            model=result.model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cached_tokens=result.cached_tokens,
            reasoning_tokens=result.reasoning_tokens,
            image_tokens=result.image_tokens,
            search_requests=result.search_requests,
            search_cost=result.search_cost,
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
            f"｜聯網搜尋（{search_mode}）" if searching else "",
            f"｜讀取 {len(page_blocks)} 個連結" if page_blocks else "",
        )
        return ChatOutcome(
            text=result.text,
            result=result,
            sticker_file_id=sticker_file_id,
            reasoning=reasoning_text,
        )
