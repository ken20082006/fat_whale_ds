"""人設語氣的抽樣檢查。

跑一段連續對話，把回覆印出來供人判讀。跨多輪才看得出「不要每則都帶刺」
這類規則有沒有生效 —— 單看一則回覆是看不出節奏的。

用法：
    .venv/Scripts/python scripts/tone_check.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dafeijing.core.persona import Persona, PersonaContext  # noqa: E402
from dafeijing.llm.openrouter import LLMError, OpenRouterClient  # noqa: E402
from dafeijing.settings import Settings  # noqa: E402

# 涵蓋各種場合：打招呼、技術任務、追問、示弱、道謝、徵詢意見
TURNS = [
    "你好",
    "幫我寫一個 Python 函式，把秒數轉成 時:分:秒 的格式",
    "為什麼用 divmod 而不是自己算？",
    "我啱啱開始學 Python，有啲地方睇唔明",
    "謝謝你，幫咗我好大忙",
    "你覺得我應該學 Rust 嗎？",
]


async def main() -> int:
    cfg = Settings()  # type: ignore[call-arg]
    if not cfg.openrouter_api_key:
        print("FW_OPENROUTER_API_KEY 是空的，請先在 .env 填入。")
        return 1

    persona = Persona.load(cfg.persona_file)
    system_prompt = persona.build(PersonaContext(display_name="Ken"))

    print(f"人設來源：{persona.source}")
    print(f"模型：{cfg.model}\n")

    client = OpenRouterClient(cfg)
    messages: list[dict] = [{"role": "system", "content": system_prompt}]

    try:
        for index, turn in enumerate(TURNS, start=1):
            messages.append({"role": "user", "content": turn})
            try:
                result = await client.chat(messages, max_tokens=800, reasoning=False)
            except LLMError as exc:
                print(f"[{index}] 呼叫失敗：{exc}\n")
                continue

            messages.append({"role": "assistant", "content": result.text})

            print(f"───── 第 {index} 輪 ─────")
            print(f"你：{turn}")
            print(f"大肥鯨：{result.text}")
            print(f"（{result.completion_tokens} tokens，${result.cost:.6f}）\n")

    finally:
        await client.close()

    print("═" * 50)
    print("判讀重點：有沒有哪一則刺得太過？連續兩則都帶刺？技術題有沒有好好答？")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
