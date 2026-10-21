"""DeepSeek Harness 嘅 SDK（stdio JSON-RPC）客戶端。

## 為什麼用 SDK 而唔用 HTTP

dsh 個 web HTTP API 有 process-scoped 認證：

- `launchToken` 每條 process 都係 `randomBytes(32)` 隨機生成，冇環境變數可以
  固定（見 `dsh-client-connection` 嘅 `processLaunchToken`）。
- Cookie 綁定**本 process 內嘅 in-memory peer**（`peers.get(id)`），
  所以就算存起 cookie，dsh 一重啟就全部失效。
- `Config` 只有 `recovery` / `trustedHosts` / `cookieMaxAgeDays` /
  `maxRequestBodyBytes` —— **冇任何開關可以關掉認證**。

即係想經 HTTP 控 dsh，每次重啟都要由 log 刮 token。太脆。

SDK profile 用 stdio，**信任邊界就係 process 本身**，冇 token、冇 cookie。

## 協議

newline-delimited JSON-RPC 2.0，一行一個 frame。

客戶端 → 伺服器：

    initialize      {cwd, provider, model, maxTokens?}
    session/prompt  {sessionId, contentBlocks}
    shutdown        {}

伺服器 → 客戶端（notification）:

    session/event   {sessionId, event}      ← 完整 session log
    session/status  {sessionId, status}     ← "idle" | "running"
    subagent/started, subagent/finished

⚠️ **冇 cancel、冇 session close** —— 中止一輪要靠關掉 process
（官方 protocol README 明講）。

## 一輪點樣完

`session/prompt` 只回一個入隊收據（`messageId`），唔係答案。
答案要喺 `session/event` 度等自己嗰條 session 嘅 `turn/end`：

    {"type": "assistant/message", "data": {"message": {"content": [...]}}}
    {"type": "turn/end", "data": {"turn": 1, "reason": {"kind": "completed"}}}

`reason.kind == "error"` 就當失敗。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 一輪可能行幾十秒（推理、壓縮）。同 Hermes 年代一致。
DEFAULT_TIMEOUT = 180.0

# 啟動最多等幾久。dsh 要載入成個 Cordis plugin tree。
STARTUP_TIMEOUT = 90.0

# asyncio StreamReader 嘅每行上限。
# 預設 64 KiB 太細 —— dsh 一則 JSON-RPC frame 可以好大（長回覆、
# 或者 `session.event` 帶完整 message），爆咗會拋
# `Separator is found, but chunk is longer than limit`，
# 然後成條 read loop 死，之後所有回覆都收唔到。
#
# 2026-10-20 真機爆咗一次（room 群組長對話）。
# 16 MiB 足夠任何正常回覆（`maxTokens` 上限 32768，即最多幾百 KB）。
_STREAM_LIMIT = 16 * 1024 * 1024

# Telegram 啲 data URL 長度可以好大（一張 1280px JPEG 約 200KB base64）。
_MAX_IMAGE_BYTES = 8 * 1024 * 1024

_DATA_URL = re.compile(r"^data:(image/[a-z+.-]+);base64,(.*)$", re.IGNORECASE | re.DOTALL)

# dsh 個圖片 block 只收呢幾種（見 apiproxy 嘅 `promptContentPartSchema`）。
_ALLOWED_MEDIA = {"image/png", "image/jpeg", "image/webp", "image/gif"}

# 模型「想完就停」時用嘅回覆。
#
# 唔好用「出咗狀況」—— 嗰句係 Router 嘅錯誤訊息，但呢個 situation
# 其實 turn 正常完成，只係模型冇講嘢。用一句中性嘅，順便叫對方
# 再講一次。
EMPTY_REPLY = "本鯨頭先諗得太入神，唔記得講嘢。你再講一次？"


class DshError(RuntimeError):
    """dsh 拒絕、答唔到，或者條 process 死咗。"""


@dataclass(frozen=True)
class Reply:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class _Turn:
    """一輪嘅收集器。收到事件就填。"""

    session_id: str
    text: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    done: asyncio.Event = field(default_factory=asyncio.Event)
    error: str | None = None


# ── 內容砌法 ────────────────────────────────────────────


def _data_url_to_part(url: str) -> dict[str, Any] | None:
    """`data:image/jpeg;base64,...` → dsh SDK 嘅 image block。

    ⚠️ **欄位名係 `mimeType`，唔係 `mediaType`。**
    SDK server（`dsh-sdk-jsonrpc-server`）係咁讀嘅：

        mediaType: image.mimeType      // 見 lib/index.js 嘅 durablePromptContent

    寫錯咗會變成 `undefined`，然後回：
        Image type undefined is not accepted by this deployment.
    （2026-10-20 真機撞到 —— 圖片同貼圖全部失敗。）
    注意：HTTP API 嗰邊（`PromptContentPart`）係用 `mediaType`，
    兩套唔同，唔好撈亂。

    `data` 係**純 base64**，唔可以帶 `data:` prefix。

    格式唔啱或者太大就回 None —— 圖片係加分項，唔可以令整則訊息送唔到。
    """
    match = _DATA_URL.match(url.strip())
    if not match:
        logger.warning("圖片唔係 data URL，略過：%s", url[:60])
        return None
    media_type, data = match.group(1).lower(), match.group(2).strip()
    if media_type not in _ALLOWED_MEDIA:
        logger.warning("dsh 唔收 %s，略過", media_type)
        return None
    if len(data) > _MAX_IMAGE_BYTES * 4 // 3:
        logger.warning("圖片太大（base64 %d bytes），略過", len(data))
        return None
    return {"type": "image", "mimeType": media_type, "data": data}


def build_content(text: str, images: list[str] | None) -> list[dict[str, Any]]:
    """砌 `session/prompt` 嘅 `contentBlocks`。"""
    parts: list[dict[str, Any]] = []
    if text.strip():
        parts.append({"type": "text", "text": text})
    for url in images or []:
        part = _data_url_to_part(url)
        if part is not None:
            parts.append(part)
    return parts


# ── 事件解析 ────────────────────────────────────────────


def _text_of(event: dict[str, Any]) -> str | None:
    """由 `assistant/message` 事件抽文字。冇文字就回 None。"""
    message = (event.get("data") or {}).get("message") or {}
    blocks = message.get("content")
    if not isinstance(blocks, list):
        return None
    chunks = [
        b.get("text", "")
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()
    ]
    return "\n".join(chunks) if chunks else None


def _event_usage(event: dict[str, Any]) -> tuple[int, int]:
    """由 `assistant/message` 事件攞 token。

    dsh 嘅 `TokenUsage` 欄位名唔穩定（camel / snake 都見過），所以逐個試。
    搵唔到就回 0 —— 成本記錄唔應該令回覆失敗。
    """
    usage = (event.get("data") or {}).get("usage") or {}
    inp = usage.get("inputTokens") or usage.get("input_tokens") or usage.get("promptTokens") or 0
    out = usage.get("outputTokens") or usage.get("output_tokens") or usage.get("completionTokens") or 0
    try:
        return int(inp), int(out)
    except (TypeError, ValueError):
        return 0, 0


def _turn_error(event: dict[str, Any]) -> str | None:
    """`turn/end` 帶錯誤就回一段人睇得明嘅字。"""
    reason = (event.get("data") or {}).get("reason")
    if not isinstance(reason, dict) or reason.get("kind") != "error":
        return None
    error = reason.get("error") or {}
    if isinstance(error, dict):
        code = error.get("code") or "error"
        message = error.get("message") or ""
        return f"{code}: {message}".strip(": ")
    return str(error)


class DshClient:
    """一條長駐嘅 `dsh --profile <name>` 子進程 + 佢嘅 JSON-RPC 對話。"""

    def __init__(
        self,
        command: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        startup_timeout: float = STARTUP_TIMEOUT,
    ):
        self._command = command
        self._cwd = cwd
        self._env = env
        self._timeout = timeout
        self._startup_timeout = startup_timeout

        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._stderr_reader: asyncio.Task | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._turns: dict[str, _Turn] = {}
        # 今次 process 內已經處理過（resume 過或者開過）嘅 session。
        # 純記憶體 —— Router 重啟後會再試一次 resume，正確。
        self._known_sessions: set[str] = set()
        self._initialized = False
        self._closed = False
        self._stderr_tail: list[str] = []

    # ── 生命週期 ────────────────────────────────────────

    async def start(self) -> None:
        """起 process 並做 `initialize`。重複叫冇害。"""
        if self._initialized:
            return

        # ⚠️ **唔可以只睇 `returncode`。**
        # stdout 讀取失敗（例如行太長）會令 read loop 死咗，但條
        # process 可能仍然生存 —— 咁樣寫入 stdin 都冇人讀，請求會
        # 永遠等唔到回應。所以舊 process 一律殺咗再起。
        if self._proc is not None:
            if self._proc.returncode is None:
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
                except Exception:  # noqa: BLE001
                    pass
            self._proc = None
        for task in (self._reader, self._stderr_reader):
            if task is not None and not task.done():
                task.cancel()
        self._reader = self._stderr_reader = None

        self._closed = False
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
                env=self._env,
                # ⚠️ **一定要加大 `limit`。**
                # asyncio 嘅 StreamReader 預設每行上限 64 KiB；超過就拋
                # `Separator is found, but chunk is longer than limit`，
                # 成條 read loop 死，之後所有回覆都收唔到。
                #
                # dsh 一則 SSE 風格嘅 JSON-RPC frame 可以好大 ——
                # 長回覆、或者 `session.event` 帶完整 message 內容。
                # 2026-10-20 真機爆咗一次（room 群組長對話）。
                limit=_STREAM_LIMIT,
            )
        except FileNotFoundError as exc:
            raise DshError(f"搵唔到 dsh：{self._command[0]}") from exc

        self._reader = asyncio.create_task(self._read_loop(), name="dsh-stdout")
        self._stderr_reader = asyncio.create_task(self._drain_stderr(), name="dsh-stderr")

        try:
            await asyncio.wait_for(
                self._request(
                    "initialize",
                    {
                        "cwd": self._cwd or "",
                        "provider": "openrouter",
                        "model": "deepseek/deepseek-v4.1-flash-1017",
                    },
                ),
                timeout=self._startup_timeout,
            )
        except asyncio.TimeoutError as exc:
            await self.close()
            raise DshError(
                f"dsh 啟動逾時（{self._startup_timeout:.0f}s）"
                + (f"｜stderr：{self._stderr_hint()}" if self._stderr_tail else "")
            ) from exc
        except Exception:
            await self.close()
            raise
        self._initialized = True
        logger.info("dsh 已就緒（pid=%s）", self._proc.pid if self._proc else "?")

    async def close(self) -> None:
        """收工。盡量 graceful，唔得就殺。"""
        self._closed = True

        if self._proc is not None and self._proc.returncode is None:
            # 先試 protocol shutdown，再關 stdin（SDK profile 綁咗 EOF 就退）。
            try:
                await asyncio.wait_for(self._request("shutdown", {}), timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
            try:
                if self._proc.stdin is not None and not self._proc.stdin.is_closing():
                    self._proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._proc.kill()
                with_suppress = asyncio.shield(self._proc.wait())
                try:
                    await asyncio.wait_for(with_suppress, timeout=5.0)
                except Exception:  # noqa: BLE001
                    pass

        for task in (self._reader, self._stderr_reader):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._reader = self._stderr_reader = None
        self._proc = None
        self._initialized = False
        self._fail_pending("dsh 已關閉")

    def _stderr_hint(self) -> str:
        return " / ".join(self._stderr_tail[-3:])[:400]

    async def _drain_stderr(self) -> None:
        """食晒 stderr，留最後幾行做錯誤提示。唔食會塞爆 pipe。

        ⚠️ **用 INFO 而唔係 DEBUG。** dsh 將模型呼叫失敗、plugin 錯誤
        全部寫 stderr，睇唔到就等於盲。呢啲行每則訊息最多幾行，
        唔會洗版。
        """
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            async for raw in proc.stderr:
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    self._stderr_tail.append(line)
                    del self._stderr_tail[:-40]
                    logger.info("dsh stderr: %s", line)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass

    # ── 傳輸 ────────────────────────────────────────────

    async def _read_loop(self) -> None:
        """讀 stdout 每一行 JSON。"""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    frame = json.loads(line)
                except ValueError:
                    # ⚠️ stdout 只應該有 JSON-RPC。有別嘅嘢就係 plugin 污染咗
                    # stdout（官方都知道呢個風險），記錄但唔好死。
                    logger.warning("dsh stdout 有非 JSON：%s", line[:200])
                    continue
                self._handle_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("dsh stdout 讀取失敗：%s", exc)
        finally:
            # ⚠️ **一定要重設 `_initialized`。**
            # 唔重設嘅話，process 死咗之後 `ask()` 見到
            # `_initialized is True` 就唔會重開，之後每則都失敗。
            # 2026-10-20 真機撞到（stdout 行太長 → read loop 死）。
            self._initialized = False
            if not self._closed:
                hint = self._stderr_hint()
                reason = f"：{hint}" if hint else ""
                self._fail_pending(f"dsh process 結束{reason}")

    def _handle_frame(self, frame: dict[str, Any]) -> None:
        # 回應：有 id、冇 method
        if "id" in frame and "method" not in frame:
            future = self._pending.pop(frame["id"], None)
            if future is not None and not future.done():
                if "error" in frame:
                    error = frame["error"] or {}
                    future.set_exception(
                        DshError(f"dsh 報錯 [{error.get('code')}]：{error.get('message')}")
                    )
                else:
                    future.set_result(frame.get("result"))
            return

        # notification：有 method、冇 id
        #
        # ⚠️ **method 名係點號，唔係斜線。**
        # `session.event` / `session.status` —— 但 *request* 係
        # `session/prompt` / `session/resume`（斜線）。兩套唔同，
        # 2026-10-20 寫錯咗斜線，結果所有回覆都被靜靜丟棄。
        method = frame.get("method")
        params = frame.get("params") or {}
        if method == "session.event":
            self._on_event(params.get("sessionId"), params.get("event") or {})
        elif method == "session.status":
            if params.get("status") == "running":
                logger.debug("dsh %s running", params.get("sessionId"))

    def _on_event(self, session_id: str | None, event: dict[str, Any]) -> None:
        if not session_id:
            return
        turn = self._turns.get(session_id)
        if turn is None:
            return
        kind = event.get("type")
        if kind == "assistant/message":
            text = _text_of(event)
            if text:
                turn.text.append(text)
            inp, out = _event_usage(event)
            turn.input_tokens += inp
            turn.output_tokens += out
        elif kind == "turn/end":
            error = _turn_error(event)
            if error:
                turn.error = error
            turn.done.set()

    def _fail_pending(self, message: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(DshError(message))
        self._pending.clear()
        for turn in self._turns.values():
            if not turn.done.is_set():
                turn.error = message
                turn.done.set()

    async def _write(self, frame: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            raise DshError("dsh process 唔喺度")
        line = json.dumps(frame, ensure_ascii=False) + "\n"
        proc.stdin.write(line.encode("utf-8"))
        await proc.stdin.drain()

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        """送一個 request，等佢個回應。"""
        self._next_id += 1
        rpc_id = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rpc_id] = future
        try:
            await self._write({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params})
            return await future
        finally:
            self._pending.pop(rpc_id, None)

    # ── 對外 API ────────────────────────────────────────

    async def _ensure_session(self, session_id: str) -> None:
        """確保 session 已經交畀 agent，或者確認佢係全新。

        分兩種情況：

        - **disk 有紀錄**（Router 重啟過）→ `session/resume`。佢會載返
          整段歷史，並修好上次中斷咗嘅 turn。
        - **乜都冇** → 唔好理，交畀 `session/prompt` 自己 `create()`。

        ⚠️ 唔可以盲試 `session/prompt` 先 —— 對已有紀錄嘅 id 會爆
        `already exists`（2026-10-20 真機撞到）。

        ⚠️ 只記住「今次 process 內處理過」。Router 重啟後第一則訊息
        會再試一次 resume —— 正確，因為嗰時就係要接返舊紀錄。
        """
        if session_id in self._known_sessions:
            return
        try:
            await self._request("session/resume", {"sessionId": session_id})
            logger.info("dsh 接返舊 session：%s", session_id)
        except DshError as exc:
            # 冇紀錄 → 交由 prompt 開新嘅。官方明講 resume
            # 「Rejects missing... never creates a replacement」，所以呢個
            # 失敗係預期之內。
            logger.debug("session/resume(%s) 唔得，當新 session：%s", session_id, exc)
        self._known_sessions.add(session_id)

    async def ask(
        self, session_id: str, text: str, images: list[str] | None = None
    ) -> Reply:
        """送一則入去指定 session，等佢講完，回傳文字。

        ⚠️ 一定要先 `start()`。

        ## 為什麼要先 `session/resume`

        `session/prompt` 對未見過嘅 id 會用 `agents.create()`。但如果嗰條
        id **已經喺 disk 有紀錄**（例如 Router 重啟過），`create()`
        會爆 `session "..." already exists` —— 呢個係 2026-10-20 真機
        測試撞到嘅。

        所以流程係：

            第一次見（process 內）→ 試 session/resume
                                     失敗（冇紀錄）→ session/prompt 自動開
            之後同一條 → 直茬 session/prompt

        ## ⚠️ resume 會重播歷史

        `session/resume` 一載入就會將 session 嘅 log 事件（包括上一輪
        嘅 `assistant/message` 同 `turn/end`）推返出嚟。所以**一定要
        resume 完之後才註冊 turn collector**，否則會即刻收到一條舊嘅
        `turn/end`，`wait()` 立即返回但文字係空 —— 結果就係卡住。

        （2026-10-20 真機撞到：dsh 明明答咗，Router 完全冇送出。）
        """
        content = build_content(text, images)
        if not content:
            raise DshError("冇內容可以送（文字同圖都空）")
        if not self._initialized:
            await self.start()

        if session_id in self._turns and not self._turns[session_id].done.is_set():
            raise DshError(f"session {session_id} 已經有一輪喺度跑")

        # ⚠️ 次序係關鍵：resume **先**，collector **後**。
        # 調轉嘅話會食到 resume 重播嘅歷史事件。
        await self._ensure_session(session_id)

        turn = _Turn(session_id=session_id)
        self._turns[session_id] = turn
        try:
            await self._request(
                "session/prompt",
                {"sessionId": session_id, "contentBlocks": content},
            )
            try:
                await asyncio.wait_for(turn.done.wait(), timeout=self._timeout)
            except asyncio.TimeoutError as exc:
                raise DshError(f"等 dsh 回覆逾時（{self._timeout:.0f}s）") from exc

            if turn.error:
                raise DshError(turn.error)

            answer = "\n".join(turn.text).strip()
            if not answer:
                # 模型有時會「想完就停」—— turn 正常完成，但只出咗
                # reasoning、冇 text block（`stopReason: "stop"`）。
                # 2026-10-20 群組真機撞到一次。
                #
                # 呢個唔算錯誤：dsh 本身認為成功咗。所以用一個中性嘅
                # 內建回覆，唔好拋錯令 Router 講「出咗狀況」。
                logger.warning(
                    "dsh %s turn 完成但冇文字（只有 reasoning）", session_id
                )
                answer = EMPTY_REPLY

            reply = Reply(
                text=answer,
                input_tokens=turn.input_tokens,
                output_tokens=turn.output_tokens,
            )
            logger.info(
                "dsh %s ← %d in / %d out%s",
                session_id,
                reply.input_tokens,
                reply.output_tokens,
                f"（{len(images)} 張圖）" if images else "",
            )
            return reply
        finally:
            self._turns.pop(session_id, None)
