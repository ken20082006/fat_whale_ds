"""測新人設：彩蛋、節奏、做事準確度。"""
import asyncio, sys
sys.path.insert(0, "/app")
from dafeijing.router.dsh import DshClient, DshError

PROMPTS = [
    "大肥魚，你覺得自己肥唔肥？",
    "你又摸魚？",
    "幫我計 123 × 45 等於幾多。",
]

async def main() -> int:
    c = DshClient(["/usr/local/bin/dsh-entrypoint"], timeout=150, startup_timeout=90)
    try:
        await c.start()
        for i, p in enumerate(PROMPTS, 1):
            r = await c.ask("probe-persona-3", p)
            print(f"[{i}] 用戶：{p}")
            print(f"    大肥鯨：{r.text[:300]}\n")
        return 0
    except DshError as e:
        print("FAIL:", str(e)[:400]); return 1
    finally:
        await c.close()

raise SystemExit(asyncio.run(main()))
