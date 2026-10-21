"""開一條新 session，問一句，用嚟檢查 tool list。"""
import asyncio, sys
sys.path.insert(0, "/app")
from dafeijing.router.dsh import DshClient, DshError

async def main() -> int:
    c = DshClient(["/usr/local/bin/dsh-entrypoint"], timeout=120, startup_timeout=90)
    try:
        await c.start()
        r = await c.ask("probe-tools-check-3", "講一句你好就得。")
        print("REPLY:", r.text[:200])
        return 0
    except DshError as e:
        print("FAIL:", str(e)[:400]); return 1
    finally:
        await c.close()

raise SystemExit(asyncio.run(main()))
