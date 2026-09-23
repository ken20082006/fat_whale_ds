"""OpenRouter 用戶端。

用量一律以 API 回傳的 usage 欄位為準，不用本地估算 ——
本地估算只用於送出前決定要不要壓縮歷史。
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """對使用者友善的錯誤。message 可直接顯示在 Telegram 上。"""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class LLMResult:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    image_tokens: int = 0
    cost: float = 0.0
    finish_reason: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


_RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 520, 522, 524}


class OpenRouterClient:
    def __init__(self, cfg) -> None:
        self._cfg = cfg
        self._client = httpx.AsyncClient(
            base_url=cfg.openrouter_base_url.rstrip("/"),
            timeout=httpx.Timeout(cfg.request_timeout_seconds, connect=15.0),
            headers={
                "Authorization": f"Bearer {cfg.openrouter_api_key}",
                "HTTP-Referer": cfg.openrouter_referer,
                "X-Title": cfg.openrouter_title,
                "Content-Type": "application/json",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    # ── 主要入口 ────────────────────────────────────────

    async def chat(
        self,
        messages: list[dict],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 1.0,
        reasoning: bool | None = None,
        web_search: bool = False,
    ) -> LLMResult:
        payload = {
            "model": model or self._cfg.model,
            "messages": messages,
            "temperature": temperature,
            "usage": {"include": True},
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens

        # 這個模型預設會做推理，而推理 token 以輸出計價、且會佔用 max_tokens 額度。
        # 只有明確要關的時候才送參數；其他值（effort=low/minimal）實測無效。
        if reasoning is False:
            payload["reasoning"] = {"enabled": False}

        # 聯網搜尋。由 OpenRouter 代為搜尋，結果以摘要形式注入並附上來源標註。
        # 這是獨立的計費項目，與 token 分開算。
        if web_search:
            plugin: dict = {
                "id": "web",
                "engine": self._cfg.search_engine,
                "max_results": self._cfg.search_max_results,
                # 外掛的預設指示會要模型標出來源（格式像 `(網域 (網址))`），
                # 那是它的預設行為，不是模型自己愛貼。這裡直接覆蓋掉。
                "search_prompt": (
                    "根據以下搜尋結果回答問題。"
                    "不要輸出網址、來源連結或出處標註，直接陳述事實即可。"
                ),
            }
            if self._cfg.search_engine_mode:
                plugin["mode"] = self._cfg.search_engine_mode
            payload["plugins"] = [plugin]

        data = await self._post(payload)
        return self._parse(data)

    async def summarise(self, prompt: str, max_tokens: int | None = None) -> str:
        """內部工作（摘要／壓縮）走便宜的模型，且不需要人設或推理。"""
        result = await self.chat(
            [{"role": "user", "content": prompt}],
            model=self._cfg.model_utility,
            max_tokens=max_tokens or self._cfg.summary_max_tokens,
            temperature=0.3,
            reasoning=False,
        )
        return result.text

    # ── 傳輸 ────────────────────────────────────────────

    async def _post(self, payload: dict) -> dict:
        last_error: Exception | None = None

        for attempt in range(self._cfg.max_retries):
            try:
                response = await self._client.post("/chat/completions", json=payload)

                if response.status_code in _RETRY_STATUS:
                    last_error = LLMError(
                        f"上游暫時無法服務（HTTP {response.status_code}）", retryable=True
                    )
                    await self._backoff(attempt, response.headers.get("retry-after"))
                    continue

                if response.status_code >= 400:
                    detail = _extract_error(response)
                    logger.error("OpenRouter 回傳 %s：%s", response.status_code, detail)
                    raise LLMError(_friendly_error(response.status_code, detail))

                return response.json()

            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                logger.warning("連線失敗（第 %d 次）：%s", attempt + 1, exc)
                await self._backoff(attempt, None)

        logger.error("重試耗盡：%s", last_error)
        raise LLMError("連線不穩，本鯨連不上腦子。稍後再試一次。")

    async def _backoff(self, attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = 0.0
        else:
            delay = min(2.0 ** attempt, 20.0)
        delay += random.uniform(0, 0.5)
        await asyncio.sleep(delay)

    def _parse(self, data: dict) -> LLMResult:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("模型沒有回傳內容。")

        choice = choices[0]
        message = choice.get("message") or {}
        text = (message.get("content") or "").strip()

        usage = data.get("usage") or {}
        prompt_details = usage.get("prompt_tokens_details") or {}
        completion_details = usage.get("completion_tokens_details") or {}
        reasoning_tokens = int(completion_details.get("reasoning_tokens") or 0)

        if not text:
            # 推理模型可能把整個額度花在思考上，導致 content 是空的。
            if choice.get("finish_reason") == "length" or reasoning_tokens:
                raise LLMError(
                    "本鯨想得太久，額度用完了。縮短問題，或用 /think off 關掉深度思考。"
                )
            if message.get("refusal"):
                raise LLMError("模型拒絕回答這個問題。")
            raise LLMError("模型回傳了空白內容。")

        return LLMResult(
            text=text,
            model=data.get("model") or "unknown",
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            cached_tokens=int(prompt_details.get("cached_tokens") or 0),
            reasoning_tokens=reasoning_tokens,
            image_tokens=int(completion_details.get("image_tokens") or 0),
            cost=float(usage.get("cost") or 0.0),
            finish_reason=choice.get("finish_reason"),
        )


def _extract_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except Exception:
        return response.text[:300]
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    return str(error or body)[:300]


def _friendly_error(status: int, detail: str) -> str:
    if status == 401:
        return "API key 好像不對，本鯨進不去。"
    if status == 402:
        return "OpenRouter 餘額不足，本鯨沒飯吃了。"
    if status == 403:
        return "這個模型沒有存取權限。"
    if status == 404:
        return "找不到指定的模型，請檢查設定中的模型名稱。"
    if status == 429:
        return "請求太密集了，等一下再試。"
    return f"上游出錯（HTTP {status}）：{detail}"
