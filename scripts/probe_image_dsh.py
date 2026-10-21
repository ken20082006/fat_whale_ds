"""經 dsh 送一張圖，睇模型讀唔讀得啱（短上下文）。"""
import asyncio, base64, sys
sys.path.insert(0, "/app")
from dafeijing.router.dsh import DshClient, DshError

AID = "1fc64bbfdd69e45bb662818cd2d9f49a881312182a177fb04b3019d45b6b05af"
PATH = f"/opt/dsh/attachments/v1/objects/{AID[:2]}/{AID}"

async def main() -> int:
    raw = open(PATH, "rb").read()
    url = "data:image/jpeg;base64," + base64.b64encode(raw).decode()
    c = DshClient(["/usr/local/bin/dsh-entrypoint"], timeout=180, startup_timeout=90)
    try:
        await c.start()
        r = await c.ask("probe-img-dsh", "呢張圖寫咩？用兩句講。", images=[url])
        print("REPLY:", r.text[:500])
        print("TOKENS:", r.input_tokens, "in /", r.output_tokens, "out")
        return 0
    except DshError as e:
        print("FAIL:", str(e)[:400]); return 1
    finally:
        await c.close()

raise SystemExit(asyncio.run(main()))
