"""驗證 dsh 嘅 `browser` 工具真係行得通。

流程：起一個獨立 dsh process（唔影響 Router 嗰個）→ 叫模型開
`https://example.com` 截圖 → 睇佢有冇成功。

用法（容器內）：
    docker compose exec router python scripts/probe_browser.py
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "/app")

from dafeijing.router.dsh import DshClient, DshError  # noqa: E402

PROMPT = (
    "用 browser 開以下 Google Maps 搜尋，撳入第一間店，"
    "答我：店名、評分、評論數、營業時間、地址、電話（有幾多講幾多）：\n"
    "https://www.google.com/maps/search/115+SEN+Coffee+Jordan+Hong+Kong"
)


async def main() -> int:
    client = DshClient(
        ["/usr/local/bin/dsh-entrypoint"],
        timeout=180,
        startup_timeout=90,
    )
    try:
        await client.start()
        print("dsh 已就緒", flush=True)
        reply = await client.ask("probe-maps", PROMPT)
        print("REPLY:", reply.text[:600], flush=True)
        print("TOKENS:", reply.input_tokens, "in /", reply.output_tokens, "out")
        return 0
    except DshError as exc:
        print("FAIL:", str(exc)[:800])
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
