"""OpenRouter 回應解析。

這裡的行為是照著真實回應寫的（見 scripts/ping.py）：推理模型的 content
可能是 None，而推理 token 記在 completion_tokens_details.reasoning_tokens。
"""

from __future__ import annotations

import asyncio

import pytest

from dafeijing.llm.openrouter import LLMError, OpenRouterClient


class FakeCfg:
    openrouter_base_url = "https://openrouter.ai/api/v1"
    openrouter_api_key = "test-key"
    openrouter_referer = "https://example.invalid"
    openrouter_title = "test"
    request_timeout_seconds = 5.0
    max_retries = 1
    model = "test/model"
    model_utility = "test/model"


@pytest.fixture
def client():
    instance = OpenRouterClient(FakeCfg())
    yield instance
    asyncio.run(instance.close())


def _response(content, finish_reason="stop", reasoning_tokens=0, refusal=None, cost=0.0001):
    message = {"role": "assistant", "content": content}
    if refusal:
        message["refusal"] = refusal
    return {
        "model": "deepseek/deepseek-v4.1-flash",
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": 38,
            "completion_tokens": 125,
            "cost": cost,
            "prompt_tokens_details": {"cached_tokens": 12},
            "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
        },
    }


def test_parses_text_and_usage(client):
    result = client._parse(_response("我是大肥鯨。"))

    assert result.text == "我是大肥鯨。"
    assert result.prompt_tokens == 38
    assert result.completion_tokens == 125
    assert result.cached_tokens == 12
    assert result.cost == pytest.approx(0.0001)
    assert result.total_tokens == 163


def test_parses_reasoning_tokens(client):
    result = client._parse(_response("答案", reasoning_tokens=83))
    assert result.reasoning_tokens == 83


def test_missing_details_default_to_zero(client):
    result = client._parse(
        {
            "model": "m",
            "choices": [{"message": {"content": "嗨"}, "finish_reason": "stop"}],
            "usage": {},
        }
    )
    assert result.cached_tokens == 0
    assert result.reasoning_tokens == 0
    assert result.cost == 0.0


def test_content_none_with_reasoning_raises_helpful_error(client):
    # 推理吃光額度：真實發生過，max_tokens 太小時 content 會是 None
    with pytest.raises(LLMError) as excinfo:
        client._parse(_response(None, finish_reason="length", reasoning_tokens=64))
    assert "/think" in str(excinfo.value)


def test_content_none_without_reasoning_raises(client):
    with pytest.raises(LLMError):
        client._parse(_response(None, finish_reason="length"))


def test_refusal_is_reported(client):
    with pytest.raises(LLMError) as excinfo:
        client._parse(_response(None, refusal="I cannot help with that."))
    assert "拒絕" in str(excinfo.value)


def test_no_choices_raises(client):
    with pytest.raises(LLMError):
        client._parse({"choices": []})


def test_blank_content_treated_as_empty(client):
    with pytest.raises(LLMError):
        client._parse(_response("   "))
