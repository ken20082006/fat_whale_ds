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


# ── 伺服器端工具的回應形狀 ────────────────────────────
#
# 以下照著**真實回應**寫（用 scripts/ping.py --search 探測取得），
# 不是照文件猜 —— 文件在兩處與實際不符，見各自的說明。


def _tool_response(*, cost=0.0035, inference=0.0015, searches=2, annotations=None):
    return {
        "model": "deepseek/deepseek-v4.1-flash",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "DeepSeek 最新發布的是 V4。",
                    "annotations": annotations or [],
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 4784,
            "completion_tokens": 221,
            "cost": cost,
            "cost_details": {"upstream_inference_cost": inference},
            "server_tool_use_details": {
                "web_search_requests": searches,
                "tool_calls_requested": searches,
                "tool_calls_executed": searches,
            },
        },
    }


def test_reads_search_count_from_server_tool_use_details(client):
    """欄位名是 server_tool_use_details，**不是** server_tool_use。

    照文件寫的那樣去讀會永遠拿到 0，而且是靜默失效。
    """
    assert client._parse(_tool_response(searches=3)).search_requests == 3


def test_search_cost_is_the_gap_between_total_and_inference(client):
    """usage.cost 是總額且已含搜尋費，相減就精確得到搜尋那部分。"""
    result = client._parse(_tool_response(cost=0.003474608, inference=0.001474608))
    assert result.search_cost == pytest.approx(0.002)


def test_sources_come_from_annotations(client):
    """引用在 message.annotations，不是內文標記。

    實測 start_index / end_index **全部是 0** —— 引用沒有錨定在正文位置，
    所以做不到「標在對應句子上」，最多只能附一份來源清單。
    """
    annotations = [
        {
            "type": "url_citation",
            "url_citation": {
                "url": "https://example.com/a?utm_source=parallel&utm_medium=ai",
                "title": "A",
                "start_index": 0,
                "end_index": 0,
            },
        },
        {
            "type": "url_citation",
            "url_citation": {"url": "https://example.com/a", "title": "重複"},
        },
        {"type": "url_citation", "url_citation": {"url": "https://www.other.hk/b"}},
    ]
    result = client._parse(_tool_response(annotations=annotations))

    # 追蹤參數要剝掉，重複的只留一次
    assert result.sources == ["https://example.com/a", "https://www.other.hk/b"]


def test_array_content_is_flattened(client):
    """content 可能是陣列。

    實測帶工具時它是字串，但文件兩種都寫過 —— 直接對陣列呼叫 .strip()
    會 AttributeError。
    """
    result = client._parse(
        {
            "model": "m",
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "text", "text": "前半"},
                            {"type": "text", "text": "後半"},
                        ]
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {},
        }
    )
    assert result.text == "前半後半"


def test_tool_calls_without_text_gives_a_specific_message(client):
    """叫了工具但沒有正文時，不要說「額度用完」—— 那不是原因。"""
    with pytest.raises(LLMError) as excinfo:
        client._parse(
            {
                "model": "m",
                "choices": [
                    {"message": {"content": ""}, "finish_reason": "tool_calls"}
                ],
                "usage": {},
            }
        )
    assert "查完" in str(excinfo.value)


def test_length_with_searches_does_not_blame_reasoning(client):
    """查完之後寫不完，跟「想得太久」是兩回事 —— 叫人關推理幫不上忙。"""
    with pytest.raises(LLMError) as excinfo:
        client._parse(
            {
                "model": "m",
                "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                "usage": {
                    "server_tool_use_details": {"web_search_requests": 2},
                    "completion_tokens_details": {},
                },
            }
        )
    assert "問窄" in str(excinfo.value)
    assert "think off" not in str(excinfo.value)
