"""DeepSeek Harness SDK 客戶端 —— stdio JSON-RPC。

呢度嘅測試用一個**假 dsh process**：一個 node（其實係 python）腳本，
讀 stdin 嘅 JSON-RPC、寫 stdout。咁樣連 process 生命週期同 pipe 行為都
驗得到，唔使 mock 到見唔到真嘢。

⚠️ 真嘅 dsh 喺整合測試（手動）才試。
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from pathlib import Path

import pytest

from dafeijing.router.dsh import (
    DshClient,
    DshError,
    Reply,
    _event_usage,
    _text_of,
    _turn_error,
    build_content,
)


# ── 砌事件 ──────────────────────────────────────────────


def text_event(text: str, usage: dict | None = None) -> dict:
    event: dict = {
        "type": "assistant/message",
        "data": {"message": {"content": [{"type": "text", "text": text}]}},
    }
    if usage is not None:
        event["data"]["usage"] = usage
    return event


def turn_end(reason: dict | None = None) -> dict:
    return {"type": "turn/end", "data": {"turn": 1, "reason": reason or {"kind": "completed"}}}


# ── build_content ───────────────────────────────────────


def test_text_only():
    assert build_content("你好", None) == [{"type": "text", "text": "你好"}]


def test_empty_text_is_dropped():
    assert build_content("   ", None) == []


def test_image_data_url_becomes_an_image_block():
    assert build_content("呢張", ["data:image/jpeg;base64,QUJD"]) == [
        {"type": "text", "text": "呢張"},
        {"type": "image", "mediaType": "image/jpeg", "data": "QUJD"},
    ]


def test_image_only_is_allowed():
    assert build_content("", ["data:image/png;base64,QUJD"]) == [
        {"type": "image", "mediaType": "image/png", "data": "QUJD"}
    ]


def test_unsupported_media_type_is_skipped():
    """dsh 只收 png / jpeg / webp / gif。"""
    assert build_content("hi", ["data:image/bmp;base64,QUJD"]) == [
        {"type": "text", "text": "hi"}
    ]


def test_bad_data_url_is_skipped_not_fatal():
    assert build_content("hi", ["not a data url"]) == [{"type": "text", "text": "hi"}]


def test_oversized_image_is_skipped(monkeypatch):
    import dafeijing.router.dsh as mod

    monkeypatch.setattr(mod, "_MAX_IMAGE_BYTES", 4)
    assert build_content("hi", ["data:image/jpeg;base64," + "A" * 100]) == [
        {"type": "text", "text": "hi"}
    ]


# ── 事件解析 ────────────────────────────────────────────


def test_text_of_joins_blocks():
    event = {
        "data": {
            "message": {
                "content": [
                    {"type": "text", "text": "第一段"},
                    {"type": "reasoning", "text": "唔應該攞"},
                    {"type": "text", "text": "第二段"},
                ]
            }
        }
    }
    assert _text_of(event) == "第一段\n第二段"


def test_text_of_returns_none_when_only_reasoning():
    assert _text_of({"data": {"message": {"content": [{"type": "reasoning", "text": "想"}]}}}) is None


def test_usage_reads_camel_case():
    assert _event_usage({"data": {"usage": {"inputTokens": 100, "outputTokens": 5}}}) == (100, 5)


def test_usage_reads_snake_case():
    assert _event_usage({"data": {"usage": {"input_tokens": 7, "output_tokens": 2}}}) == (7, 2)


def test_missing_usage_is_zero():
    assert _event_usage({"data": {}}) == (0, 0)


def test_turn_error_is_extracted():
    event = turn_end({"kind": "error", "error": {"code": "model-unavailable", "message": "冇模型"}})
    assert "model-unavailable" in _turn_error(event)


def test_completed_turn_has_no_error():
    assert _turn_error(turn_end()) is None


# ── 假 dsh process ──────────────────────────────────────


FAKE_DSH = textwrap.dedent(
    '''
    import json, sys

    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        frame = json.loads(line)
        method = frame.get("method")
        rpc_id = frame.get("id")

        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rpc_id, "result": {"serverInfo": {"name": "fake"}}})
        elif method == "session/prompt":
            sid = frame["params"]["sessionId"]
            send({"jsonrpc": "2.0", "id": rpc_id, "result": {"messageId": "m1"}})
            send({"jsonrpc": "2.0", "method": "session/event",
                  "params": {"sessionId": sid, "event": {
                      "type": "assistant/message",
                      "data": {"message": {"content": [{"type": "text", "text": "你好呀"}]},
                               "usage": {"inputTokens": 100, "outputTokens": 5}}}}})
            send({"jsonrpc": "2.0", "method": "session/event",
                  "params": {"sessionId": sid, "event": {
                      "type": "turn/end", "data": {"turn": 1, "reason": {"kind": "completed"}}}}})
        elif method == "shutdown":
            send({"jsonrpc": "2.0", "id": rpc_id, "result": {}})
            break
    '''
)


@pytest.fixture()
def fake_dsh(tmp_path: Path) -> tuple[list[str], Path]:
    script = tmp_path / "fake_dsh.py"
    script.write_text(FAKE_DSH, encoding="utf-8")
    return [sys.executable, "-u", str(script)], tmp_path


def test_ask_returns_the_assistant_text(fake_dsh):
    command, cwd = fake_dsh
    client = DshClient(command, cwd=str(cwd), timeout=15)

    async def scenario() -> Reply:
        try:
            await client.start()
            return await client.ask("grp:-100:1", "[甲|1]\nhi")
        finally:
            await client.close()

    reply = asyncio.run(scenario())
    assert reply.text == "你好呀"
    assert (reply.input_tokens, reply.output_tokens) == (100, 5)


def test_start_is_idempotent(fake_dsh):
    command, cwd = fake_dsh
    client = DshClient(command, cwd=str(cwd), timeout=15)

    async def scenario():
        await client.start()
        proc = client._proc
        await client.start()
        assert client._proc is proc
        await client.close()

    asyncio.run(scenario())


def test_ask_after_close_restarts_the_process(fake_dsh):
    """`close()` 之後再問，會自動重開 process —— 唔會靜靜失敗。

    呢個係刻意嘅：Router 唔應該因為 dsh 中途死咗就永久廢掉，
    下一則訊息要自己試返。
    """
    command, cwd = fake_dsh
    client = DshClient(command, cwd=str(cwd), timeout=15)

    async def scenario() -> Reply:
        await client.start()
        first_pid = client._proc.pid
        await client.close()
        reply = await client.ask("dm:1", "hi")
        assert client._proc is not None
        assert client._proc.pid != first_pid, "應該起咗一條新 process"
        await client.close()
        return reply

    assert asyncio.run(scenario()).text == "你好呀"


def test_missing_executable_is_a_clear_error(tmp_path: Path):
    client = DshClient([str(tmp_path / "nope")], cwd=str(tmp_path), startup_timeout=5)

    async def scenario():
        with pytest.raises(DshError, match="搵唔到"):
            await client.start()
        await client.close()

    asyncio.run(scenario())


def test_process_dying_mid_turn_surfaces(tmp_path: Path):
    """dsh 死咗，等緊嗰輪要即刻報錯，唔好乾等到逾時。"""
    script = tmp_path / "dies.py"
    script.write_text(
        textwrap.dedent(
            '''
            import json, sys, os
            for line in sys.stdin:
                frame = json.loads(line)
                if frame.get("method") == "initialize":
                    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": frame["id"],
                                                 "result": {}}) + "\\n")
                    sys.stdout.flush()
                elif frame.get("method") == "session/prompt":
                    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": frame["id"],
                                                 "result": {"messageId": "m1"}}) + "\\n")
                    sys.stdout.flush()
                    os._exit(1)
            '''
        ),
        encoding="utf-8",
    )
    client = DshClient([sys.executable, "-u", str(script)], cwd=str(tmp_path), timeout=20)

    async def scenario():
        try:
            await client.start()
            with pytest.raises(DshError):
                await client.ask("dm:1", "hi")
        finally:
            await client.close()

    asyncio.run(scenario())


def test_turn_error_from_event(fake_dsh):
    command, cwd = fake_dsh
    script = cwd / "erroring.py"
    script.write_text(
        textwrap.dedent(
            '''
            import json, sys
            for line in sys.stdin:
                frame = json.loads(line)
                m = frame.get("method")
                if m == "initialize":
                    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": frame["id"], "result": {}}) + "\\n")
                elif m == "session/prompt":
                    sid = frame["params"]["sessionId"]
                    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": frame["id"],
                                                 "result": {"messageId": "m"}}) + "\\n")
                    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": "session/event",
                        "params": {"sessionId": sid, "event": {"type": "turn/end",
                        "data": {"reason": {"kind": "error",
                        "error": {"code": "model-unavailable", "message": "冇模型"}}}}}}) + "\\n")
                elif m == "shutdown":
                    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": frame["id"], "result": {}}) + "\\n")
                    break
                sys.stdout.flush()
            '''
        ),
        encoding="utf-8",
    )
    client = DshClient([sys.executable, "-u", str(script)], cwd=str(cwd), timeout=15)

    async def scenario():
        try:
            await client.start()
            with pytest.raises(DshError, match="model-unavailable"):
                await client.ask("dm:1", "hi")
        finally:
            await client.close()

    asyncio.run(scenario())


def test_empty_answer_is_an_error(tmp_path: Path):
    script = tmp_path / "silent.py"
    script.write_text(
        textwrap.dedent(
            '''
            import json, sys
            for line in sys.stdin:
                frame = json.loads(line)
                m = frame.get("method")
                if m in ("initialize", "session/prompt"):
                    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": frame["id"],
                                                 "result": {}}) + "\\n")
                    if m == "session/prompt":
                        sid = frame["params"]["sessionId"]
                        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": "session/event",
                            "params": {"sessionId": sid, "event": {"type": "turn/end",
                            "data": {"reason": {"kind": "completed"}}}}}) + "\\n")
                elif m == "shutdown":
                    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": frame["id"], "result": {}}) + "\\n")
                    break
                sys.stdout.flush()
            '''
        ),
        encoding="utf-8",
    )
    client = DshClient([sys.executable, "-u", str(script)], cwd=str(tmp_path), timeout=15)

    async def scenario():
        try:
            await client.start()
            with pytest.raises(DshError, match="冇任何文字"):
                await client.ask("dm:1", "hi")
        finally:
            await client.close()

    asyncio.run(scenario())


def test_concurrent_turn_on_same_session_is_refused(fake_dsh):
    command, cwd = fake_dsh
    client = DshClient(command, cwd=str(cwd), timeout=15)

    async def scenario():
        try:
            await client.start()
            first = asyncio.create_task(client.ask("dm:1", "a"))
            await asyncio.sleep(0)  # 讓第一個開始
            with pytest.raises(DshError, match="已經有一輪"):
                await client.ask("dm:1", "b")
            await first
        finally:
            await client.close()

    asyncio.run(scenario())
