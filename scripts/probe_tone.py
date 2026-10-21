"""測工具答案有冇保持角色語氣。"""
import asyncio, sys
sys.path.insert(0, "/app")
from dafeijing.router.dsh import DshClient, DshError

PROMPTS = [
    "屯門公路而家塞唔塞車？",
    "佐敦有邊間咖啡店開到咁夜？",
]

async def main() -> int:
    c = DshClient(["/usr/local/bin/dsh-entrypoint"], timeout=180, startup_timeout=90)
    try:
        await c.start()
        for i, p in enumerate(PROMPTS, 1):
            r = await c.ask("probe-tone", p)
            print(f"[{i}] 用戶：{p}")
            print(f"    本鯨：{r.text[:400]}\n")
        return 0
    except DshError as e:
        print("FAIL:", str(e)[:400]); return 1
    finally:
        await c.close()

raise SystemExit(asyncio.run(main()))
