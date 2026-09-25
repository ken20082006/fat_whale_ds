"""Hermes API client —— 回應解析、重試、錯誤處理。

唔打真網路：用 httpx.MockTransport。真嘅 Hermes 喺整合測試（手動）先試。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from dafeijing.router.hermes import HermesClient, HermesError, Reply, extract_text


def _message(text: str, *, role: str = "assistant") -> dict:
    return {
        "type": "message",
        "role": role,
        "content": [{"type": "output_text", "text": text}],
    }


def _payload(output: list[dict], **extra) -> dict:
    return {"id": "resp_1", "status": "completed", "output": output, **extra}


# ── extract_text ────────────────────────────────────────


def test_extracts_the_assistant_message():
    assert extract_text(_payload([_message("你好呀")])) == "你好呀"


def test_skips_reasoning_and_tool_calls():
    """一用過工具，第一個 item 就係 function_call —— 攞 output[0] 會回空話。"""
    payload = _payload(
        [
            {"type": "reasoning", "summary": []},
            {"type": "function_call", "name": "web_search", "arguments": "{}"},
            _message("查完喇，答案係 42。"),
        ]
    )
    assert extract_text(payload) == "查完喇，答案係 42。"


def test_joins_multiple_message_items():
    payload = _payload([_message("第一段"), _message("第二段")])
    assert extract_text(payload) == "第一段\n第二段"


def test_ignores_non_output_text_parts():
    payload = _payload(
        [
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "reasoning_text", "text": "唔應該攞"},
                    {"type": "output_text", "text": "應該攞"},
                ],
            }
        ]
    )
    assert extract_text(payload) == "應該攞"


def test_raises_when_no_message_item():
    payload = _payload([{"type": "function_call", "name": "x"}])
    with pytest.raises(HermesError, match="冇任何文字"):
        extract_text(payload)


def test_raises_when_only_whitespace():
    with pytest.raises(HermesError):
        extract_text(_payload([_message("   \n  ")]))


def test_raises_when_output_missing():
    with pytest.raises(HermesError, match="冇 output"):
        extract_text({"id": "x"})


def test_raises_when_output_is_not_a_list():
    with pytest.raises(HermesError, match="冇 output"):
        extract_text({"output": "唔係 list"})


def test_tolerates_junk_items():
    payload = _payload([None, "字串", 42, _message("真嘢")])  # type: ignore[list-item]
    assert extract_text(payload) == "真嘢"


# ── 客戶端 ──────────────────────────────────────────────


def _client(handler) -> HermesClient:
    return HermesClient(
        "http://127.0.0.1:8642", "test-key", transport=httpx.MockTransport(handler)
    )


def test_sends_the_conversation_and_attributed_input():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_payload([_message("收到")]))

    async def scenario() -> Reply:
        client = _client(handler)
        try:
            return await client.ask("grp:-100:4821", "[甲|1]\nhi")
        finally:
            await client.close()

    reply = asyncio.run(scenario())

    assert seen["url"].endswith("/v1/responses")
    assert seen["auth"] == "Bearer test-key"
    assert seen["body"] == {"input": "[甲|1]\nhi", "conversation": "grp:-100:4821"}
    assert reply.text == "收到"


def test_reads_token_usage():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_payload([_message("好")], usage={"input_tokens": 16838, "output_tokens": 1}),
        )

    async def scenario() -> Reply:
        client = _client(handler)
        try:
            return await client.ask("c", "hi")
        finally:
            await client.close()

    reply = asyncio.run(scenario())
    assert (reply.input_tokens, reply.output_tokens) == (16838, 1)


def test_missing_usage_is_zero_not_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_payload([_message("好")]))

    async def scenario() -> Reply:
        client = _client(handler)
        try:
            return await client.ask("c", "hi")
        finally:
            await client.close()

    reply = asyncio.run(scenario())
    assert (reply.input_tokens, reply.output_tokens) == (0, 0)


def test_retries_on_500_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="boom")
        return httpx.Response(200, json=_payload([_message("第二次得咗")]))

    async def scenario() -> Reply:
        client = _client(handler)
        try:
            return await client.ask("c", "hi")
        finally:
            await client.close()

    assert asyncio.run(scenario()).text == "第二次得咗"
    assert calls["n"] == 2


def test_gives_up_after_three_attempts():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    async def scenario() -> None:
        client = _client(handler)
        try:
            await client.ask("c", "hi")
        finally:
            await client.close()

    with pytest.raises(HermesError):
        asyncio.run(scenario())
    assert calls["n"] == 3


def test_does_not_retry_a_401():
    """認證錯係設定問題，重試三次只係拖時間。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"error": {"message": "Invalid gateway API key"}})

    async def scenario() -> None:
        client = _client(handler)
        try:
            await client.ask("c", "hi")
        finally:
            await client.close()

    with pytest.raises(HermesError, match="401"):
        asyncio.run(scenario())
    assert calls["n"] == 1


def test_surfaces_an_error_payload_even_with_status_200():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"message": "爆咗"}})

    async def scenario() -> None:
        client = _client(handler)
        try:
            await client.ask("c", "hi")
        finally:
            await client.close()

    with pytest.raises(HermesError, match="報錯"):
        asyncio.run(scenario())
