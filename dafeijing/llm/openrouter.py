"""OpenRouter 用戶端。

用量一律以 API 回傳的 usage 欄位為準，不用本地估算 ——
本地估算只用於送出前決定要不要壓縮歷史。
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from urllib.parse import urlparse

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
    # 模型的思考過程原文（推理開啟時才有）。實測 deepseek-v4.1-flash 會回
    # `message.reasoning`（字串）與 `message.reasoning_details`（結構化），
    # `reasoning_content` 是同義的舊名。**這裡刻意不讀 reasoning_details** ——
    # 我們只需要貼出來，不需要原樣回傳給上游。
    #
    # 預設不顯示（beta，見 settings.show_reasoning），所以這裡照樣讀回來、
    # 由呼叫端決定貼不貼；不讀的話那個開關就無從實現。
    reasoning: str = ""
    # 伺服器端工具回報的搜尋次數。欄位是 server_tool_use_details，
    # 不是 server_tool_use —— 文件沒寫，是實測出來的。
    search_requests: int = 0
    # 這一輪引用了哪些來源（去重、去追蹤參數）。只記在日誌與歷史註記，
    # 不顯示給使用者。
    sources: list[str] = field(default_factory=list)
    # usage.cost 是**總額，已含搜尋費**。這個是當中的推論部分，
    # 相減就得到搜尋費 —— 比另外估準。
    inference_cost: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def search_cost(self) -> float:
        return max(0.0, self.cost - self.inference_cost)


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
        search: bool = False,
        search_results: int | None = None,
        force_search: bool = False,
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

        # 聯網搜尋有兩條路，互斥：
        #
        # 1. **伺服器端工具**（預設）—— 模型自己決定搜幾次、用什麼查詢，
        #    而且拿到的是原文片段而不是引擎摘要。它會反覆搜（實測一次請求
        #    搜 2–4 次），所以「先撈一次冇，再換個講法撈」做得到。
        # 2. **web 外掛**（force_search）—— 保證每次請求至少搜一次，但查詢
        #    由引擎從對話推導、模型無權指定，拿到的是摘要，而且只搜一次。
        #
        # 只有需要「保證會搜」的場合（always 與 /search）才走外掛 ——
        # 伺服器端工具沒有 tool_choice，官方也沒記載可強制，所以無法用
        # 參數逼它搜。
        if force_search:
            plugin: dict = {
                "id": "web",
                "engine": self._cfg.search_engine,
                # 撈幾多條由呼叫端逐次決定（判斷出來的搜尋力度），不是寫死。
                "max_results": search_results or self._cfg.search_max_results,
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
        elif search:
            parameters: dict = {
                "engine": self._cfg.search_engine,
                "max_results": search_results or self._cfg.search_max_results,
                # 一次請求的總上限。**限結果不限請求** —— 這個參數只轉發給
                # 部分引擎，其他引擎忽略它，真正的次數上限約 3 次。
                "max_total_results": self._cfg.search_max_total_results,
            }
            if self._cfg.search_engine_mode:
                parameters["mode"] = self._cfg.search_engine_mode
            payload["tools"] = [
                {"type": "openrouter:web_search", "parameters": parameters}
            ]

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
        finish_reason = choice.get("finish_reason")
        text = _read_content(message.get("content"))

        usage = data.get("usage") or {}
        prompt_details = usage.get("prompt_tokens_details") or {}
        completion_details = usage.get("completion_tokens_details") or {}
        reasoning_tokens = int(completion_details.get("reasoning_tokens") or 0)

        # 搜尋次數在 server_tool_use_details，**不是** server_tool_use。
        # 文件沒寫清楚，是實測出來的 —— 照文件那樣讀會永遠拿到 0，
        # 而且是靜默失效。
        tool_use = usage.get("server_tool_use_details") or {}
        search_requests = int(tool_use.get("web_search_requests") or 0)

        # usage.cost 是**總額、已含搜尋費**；cost_details 裡的
        # upstream_inference_cost 才是當中的推論部分。相減就精確得到
        # 搜尋費，不必另外估。
        cost_details = usage.get("cost_details") or {}
        cost = float(usage.get("cost") or 0.0)
        inference_cost = float(cost_details.get("upstream_inference_cost") or cost)

        if not text:
            if finish_reason == "tool_calls":
                raise LLMError("本鯨查完之後來不及講，再問一次好嗎。")
            if finish_reason == "length" and search_requests:
                # 與「想得太久」不同：這裡是查完之後寫不完，叫它關推理沒有用。
                raise LLMError("本鯨查到的東西太多，講不完。問題問窄一點再試。")
            # 推理模型可能把整個額度花在思考上，導致 content 是空的。
            if finish_reason == "length" or reasoning_tokens:
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
            cost=cost,
            finish_reason=finish_reason,
            search_requests=search_requests,
            sources=_read_sources(message.get("annotations")),
            inference_cost=inference_cost,
            reasoning=_read_reasoning(message),
        )


def _read_content(raw) -> str:
    """讀出回覆正文。content 可能是字串，也可能是陣列。

    實測帶伺服器端工具時它是**字串**，但文件兩種都寫過，所以兩種都吃 ——
    直接對陣列呼叫 .strip() 會 AttributeError。
    """
    if isinstance(raw, list):
        parts = [
            part.get("text") or ""
            for part in raw
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "".join(parts).strip()
    return (raw or "").strip()


def _read_reasoning(message: dict) -> str:
    """讀出模型的思考過程原文。沒有就回空字串。

    `reasoning` 與 `reasoning_content` 是同一個東西的兩個名（官方文件明講
    後者「functions identically to」前者），不同模型／引擎回的名不一樣，
    所以兩個都吃。非字串（例如某些模型回的結構化陣列）一律當作沒有 ——
    貼出去的是給人看的文字，硬轉只會貼出一堆 JSON。
    """
    for key in ("reasoning", "reasoning_content"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _read_sources(annotations) -> list[str]:
    """從 annotations 取出引用來源。

    實測形狀是 `{"type": "url_citation", "url_citation": {"url", "title", …}}`
    的陣列。注意 `start_index` / `end_index` **全部是 0** —— 引用沒有錨定在
    正文位置，所以做不到「標在對應句子上」，最多只能附一份來源清單。
    """
    if not isinstance(annotations, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in annotations:
        if not isinstance(item, dict) or item.get("type") != "url_citation":
            continue
        citation = item.get("url_citation") or {}
        url = _clean_url(str(citation.get("url") or ""))
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _clean_url(url: str) -> str:
    """去掉追蹤參數。實測回傳的網址帶著 utm_source / utm_medium。"""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if not parsed.query:
        return url
    kept = [
        pair
        for pair in parsed.query.split("&")
        if not pair.lower().startswith(("utm_", "fbclid", "gclid"))
    ]
    return parsed._replace(query="&".join(kept)).geturl()


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
