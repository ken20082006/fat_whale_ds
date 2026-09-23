"""對話的完整流程。

私聊與群組共用同一條管線，差別只在於：群組帶入引用串、回覆上限較短、且不讀長期記憶。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..llm.openrouter import LLMResult, OpenRouterClient
from .access import AccessControl
from .persona import Persona, PersonaContext
from .session import SessionManager, SessionRef
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


class ChatService:
    def __init__(
        self,
        cfg,
        persona: Persona,
        sessions: SessionManager,
        access: AccessControl,
        llm: OpenRouterClient,
        usage: UsageLog,
    ) -> None:
        self._cfg = cfg
        self._persona = persona
        self._sessions = sessions
        self._access = access
        self._llm = llm
        self._usage = usage

    async def respond(self, req: ChatRequest) -> LLMResult:
        user = await self._access.get_user(req.tg_user_id)
        vibe = (user["vibe"] if user else None) or "mid"

        notes: list[str] = []
        if not req.is_group:
            notes = await self._sessions.notes(req.tg_user_id)

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
        messages.append({"role": "user", "content": user_content})

        max_tokens = (
            self._cfg.group_reply_max_tokens if req.is_group else self._cfg.private_reply_max_tokens
        )

        result = await self._llm.chat(messages, max_tokens=max_tokens)

        # 成功後才落庫。失敗的回合不留下痕跡，使用者重試時不會出現半截對話。
        await self._sessions.append(req.session.id, "user", user_content)
        await self._sessions.append(req.session.id, "assistant", result.text)

        await self._usage.record(
            user_id=req.tg_user_id,
            chat_id=req.chat_id,
            model=result.model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cached_tokens=result.cached_tokens,
            image_tokens=result.image_tokens,
            cost=result.cost,
        )

        try:
            await self._sessions.maybe_compact(req.session.id, self._llm.summarise)
        except Exception:
            logger.exception("壓縮歷史失敗，不影響本次回覆")

        logger.info(
            "%s → %d in / %d out（快取 %d）",
            "群組" if req.is_group else "私聊",
            result.prompt_tokens,
            result.completion_tokens,
            result.cached_tokens,
        )
        return result
