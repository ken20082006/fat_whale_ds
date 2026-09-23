"""發真實的模型呼叫，確認金鑰、模型代號與回應解析都正常。

同時比較開啟與關閉深度思考的差異 —— 推理 token 以輸出計價，
是這個模型最大的成本來源，值得親眼確認。

會產生極少量費用（兩次問答合計約 $0.0001）。
這是唯一能驗證 OpenRouter 用戶端與 usage 解析的方法，單元測試涵蓋不到。

用法：
    .venv/Scripts/python scripts/ping.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dafeijing.llm.openrouter import LLMError, OpenRouterClient  # noqa: E402
from dafeijing.settings import Settings  # noqa: E402

PROMPT = "用一句話回答：你是誰？"


async def ask(client: OpenRouterClient, reasoning: bool) -> None:
    label = "深度思考：開啟" if reasoning else "深度思考：關閉"
    print(f"───── {label} ─────")
    try:
        result = await client.chat(
            [{"role": "user", "content": PROMPT}],
            max_tokens=1024,
            reasoning=reasoning,
        )
    except LLMError as exc:
        print(f"失敗：{exc}\n")
        return

    print(f"回覆：{result.text}")
    print(
        f"  輸入 {result.prompt_tokens} / 輸出 {result.completion_tokens}"
        f"（推理 {result.reasoning_tokens}、快取 {result.cached_tokens}）"
        f"｜費用 ${result.cost:.6f}"
    )
    print()


async def main() -> int:
    cfg = Settings()  # type: ignore[call-arg]
    client = OpenRouterClient(cfg)

    print(f"模型：{cfg.model}\n")

    try:
        await ask(client, reasoning=False)
        await ask(client, reasoning=True)
    finally:
        await client.close()

    print("兩項都通，代表金鑰、模型代號、reasoning 參數與 usage 解析皆正常。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
