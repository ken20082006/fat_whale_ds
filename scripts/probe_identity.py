"""測佢知唔知自己係 DeepSeek。"""
import asyncio, sys
sys.path.insert(0, "/app")
from dafeijing.router.dsh import DshClient, DshError

PROMPTS = ["你係邊個？", "你係咪 DeepSeek？", "你用邊個模型版本？"]

async def main() -> int:
    c = DshClient(["/usr/local/bin/dsh-entrypoint"], timeout=150, startup_timeout=90)
    try:
        await c.start()
        for p in PROMPTS:
            r = await c.ask("probe-identity-2", p)
            print(f"用戶：{p}\n本鯨：{r.text[:300]}\n")
        return 0
    except DshError as e:
        print("FAIL:", str(e)[:400]); return 1
    finally:
        await c.close()

raise SystemExit(asyncio.run(main()))
