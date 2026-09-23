"""對話的完整流程。

私聊與群組共用同一條管線，差別只在於：群組帶入引用串、回覆上限較短、且不讀長期記憶。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..llm.openrouter import LLMResult, OpenRouterClient
from .access import AccessControl
from .media import PreparedImage
from .memory import MemoryExtractor
from .persona import Persona, PersonaContext
from .security import LEAK_REPLY, find_system_leak
from .session import SessionManager, SessionRef, scope_for
from .usage import UsageLog

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._cfg = cfg
        self._persona = persona
        self._sessions = sessions
        self._access = access
        self._llm = llm
        self._usage = usage
        self._memory = memory
        self.blocked_leaks = 0

    async def respond(self, req: ChatRequest) -> LLMResult:
        user = await self._access.get_user(req.tg_user_id)
        vibe = (user["vibe"] if user else None) or "mid"

        # 推理 token 以輸出計價。預設關閉，由使用者用 /think 個別覆寫。
        if user is not None and user["reasoning"] is not None:
            reasoning = bool(user["reasoning"])
        else:
            reasoning = self._cfg.reasoning_enabled

        # 長期筆記依場合分開：私聊獨立，每個群組也各自獨立。見 scope_for 的說明。
        scope = scope_for(req.is_group, req.chat_id)
        notes = await self._sessions.notes(req.tg_user_id, scope)

        system_prompt = self._persona.build(
            PersonaContext(
                vibe=vibe,
                display_name=req.display_name,
                notes=notes,
                summary=req.session.summary,
                is_group=req.is_group,
            )
        )

        history = await self._sessions.window(req.session.id)
        user_content = req.chain_text or req.text

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

        result = await self._llm.chat(messages, max_tokens=max_tokens, reasoning=reasoning)

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
            req.session.id, "user", user_content, has_image=bool(req.images)
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
            user_text=req.text or user_content,
            assistant_text=result.text,
        )

        logger.info(
            "%s → %d in / %d out（推理 %d、快取 %d）",
            "群組" if req.is_group else "私聊",
            result.prompt_tokens,
            result.completion_tokens,
            result.reasoning_tokens,
            result.cached_tokens,
        )
        return result
