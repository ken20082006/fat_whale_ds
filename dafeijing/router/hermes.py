"""Hermes API client。

Router 經呢個入去 Hermes，用 `conversation` 參數指定「邊一條串」。

    POST {base}/v1/responses
    {"input": "[陳大文|123]\n今日隻船係咪要改期？", "conversation": "grp:-100:4821"}

Hermes 會自動接上該 conversation 最新嘅一則，所以 router 唔使管歷史。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Agent 一輪可能行幾十秒（查網、睇片）。設得短會白白斷掉。
DEFAULT_TIMEOUT = 180.0

# 重試得過嘅狀態碼 —— 同大肥鯨 llm/openrouter.py 同一套判斷。
_RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 520, 522, 524}


class HermesError(RuntimeError):
    """Hermes 拒絕或者答唔到。"""


@dataclass(frozen=True)
class Reply:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


def extract_text(payload: dict[str, Any]) -> str:
    """由 Hermes 嘅回應抽出助理講嘅文字。

    回應形狀（`/v1/responses`）：

        {"output": [
            {"type": "reasoning", ...},                       # 可能冇（我哋關咗）
            {"type": "function_call", ...},                   # 用過工具就有
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "..."}]}
        ]}

    **要揀 `type == "message"` 嗰個**，唔可以淨係攞 `output[0]` ——
    一用過工具，第一個 item 就可能係 `function_call`，攞錯會回一句空話。
    """
    items = payload.get("output")
    if not isinstance(items, list):
        raise HermesError(f"回應冇 output 欄位：{str(payload)[:200]}")

    chunks: list[str] = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        if item.get("role") not in (None, "assistant"):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "output_text":
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    chunks.append(text.strip())

    if not chunks:
        raise HermesError(f"回應冇任何文字：{str(payload)[:200]}")
    return "\n".join(chunks)


def _usage(payload: dict[str, Any]) -> tuple[int, int]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return 0, 0
    return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)


class HermesClient:
    """Hermes API server 嘅薄客戶端。冇狀態 —— 對話狀態喺 Hermes 嗰邊。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
            # transport 只係為咗測試 —— 正式路徑唔傳，用 httpx 預設。
            transport=transport,
        )

    async def ask(self, conversation: str, text: str) -> Reply:
        """送一則入去指定嘅 conversation，回傳助理嘅回覆。

        `conversation` 係對話名 —— 同一條引用串要**永遠**用同一個名。
        """
        body = {"input": text, "conversation": conversation}
        response = await self._post("/v1/responses", body)
        in_tok, out_tok = _usage(response)
        reply = Reply(text=extract_text(response), input_tokens=in_tok, output_tokens=out_tok)
        logger.info(
            "Hermes %s ← %d in / %d out", conversation, in_tok, out_tok
        )
        return reply

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(3):
            try:
                response = await self._client.post(path, json=body)
            except httpx.HTTPError as exc:
                last = HermesError(f"連唔到 Hermes：{exc}")
                logger.warning("Hermes 連線失敗（第 %d 次）：%s", attempt + 1, exc)
                continue

            if response.status_code in _RETRY_STATUS:
                last = HermesError(f"Hermes 回 {response.status_code}")
                logger.warning("Hermes 可重試錯誤 %s", response.status_code)
                continue

            if response.status_code != 200:
                raise HermesError(
                    f"Hermes 回 {response.status_code}：{response.text[:300]}"
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise HermesError(f"Hermes 回咗唔係 JSON：{exc}") from exc

            if isinstance(payload, dict) and payload.get("error"):
                raise HermesError(f"Hermes 報錯：{str(payload['error'])[:300]}")
            if not isinstance(payload, dict):
                raise HermesError(f"Hermes 回咗唔係物件：{type(payload).__name__}")
            return payload

        raise last or HermesError("Hermes 連續失敗")

    async def close(self) -> None:
        await self._client.aclose()
