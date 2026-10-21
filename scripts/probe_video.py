"""驗證 dsh 嘅 `video_analyze` 工具（dsh-video-lens）。

先用 ffmpeg 整一條「紅 → 藍」嘅測試片，再叫模型分析。
如果佢講得出紅色轉藍色，就證明 vision route（MiMo）通。

用法（容器內）：
    docker compose exec router python scripts/probe_video.py
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "/app")

from dafeijing.router.dsh import DshClient, DshError  # noqa: E402

VIDEO = "/app/hermes/data/cache/videos/probe.mp4"

PROMPT = (
    f"用 video_analyze 睇下呢條片：{VIDEO}\n"
    "講下佢由頭到尾有咩變化，同埋每種顏色大概幾時出現。"
)


async def main() -> int:
    client = DshClient(
        ["/usr/local/bin/dsh-entrypoint"],
        timeout=240,
        startup_timeout=90,
    )
    try:
        await client.start()
        print("dsh 已就緒", flush=True)
        reply = await client.ask("probe-video", PROMPT)
        print("REPLY:", reply.text[:800], flush=True)
        print("TOKENS:", reply.input_tokens, "in /", reply.output_tokens, "out")
        return 0
    except DshError as exc:
        print("FAIL:", str(exc)[:900])
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
