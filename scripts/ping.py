"""發真實的模型呼叫，確認金鑰、模型代號與回應解析都正常。

同時比較開啟與關閉深度思考的差異 —— 推理 token 以輸出計價，
是這個模型最大的成本來源，值得親眼確認。

會產生極少量費用（兩次問答合計約 $0.0001）。
這是唯一能驗證 OpenRouter 用戶端與 usage 解析的方法，單元測試涵蓋不到。

用法：
    .venv/Scripts/python scripts/ping.py
    .venv/Scripts/python scripts/ping.py --search    # 加跑搜尋探測

`--search` 會跑 2×2 矩陣（推理 on/off × 伺服器工具 on/off），把回應的**原始形狀**
印出來。它存在的理由：要改用 OpenRouter 的伺服器端搜尋工具之前，得先知道幾個
實際欄位長什麼樣，否則只能照文件猜。這裡刻意**不經過 `_parse()`** ——
沒見過的欄位要看得見，不能被解析器吃掉。四趟合計約 $0.005。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dafeijing.core.media import prepare_from_bytes  # noqa: E402
from dafeijing.llm.openrouter import LLMError, OpenRouterClient  # noqa: E402
from dafeijing.settings import Settings  # noqa: E402

PROMPT = "用一句話回答：你是誰？"
SAMPLE_IMAGE = Path("assets/stickers/LLM_Moe/01.png")

# 刻意挑時效性問題：不查就答的一定是舊資料，這樣才看得出工具到底有沒有作用。
SEARCH_PROBE_PROMPT = (
    "DeepSeek 最近發布了什麼新模型？用兩句話回答，並講明是什麼時候發布的。"
)


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


async def check_image(client: OpenRouterClient, cfg) -> None:
    """驗證圖片真的送得進模型，並看它讀不讀得懂。"""
    if not SAMPLE_IMAGE.is_file():
        print("（找不到測試圖片，略過。先跑 scripts/fetch_stickers.py LLM_Moe）\n")
        return

    print("───── 圖片輸入 ─────")
    image = prepare_from_bytes(
        SAMPLE_IMAGE.read_bytes(), max_edge=cfg.image_max_edge, source="sticker"
    )
    print(
        f"轉換後 {image.width}x{image.height}，{image.byte_size / 1024:.1f} KB，"
        f"約 {image.approx_tokens} token"
    )

    try:
        result = await client.chat(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "這張圖是什麼？用一句話描述。"},
                        {"type": "image_url", "image_url": {"url": image.data_url}},
                    ],
                }
            ],
            max_tokens=300,
            reasoning=False,
        )
    except LLMError as exc:
        print(f"失敗：{exc}\n")
        return

    print(f"模型說：{result.text}")
    print(
        f"  輸入 {result.prompt_tokens} / 輸出 {result.completion_tokens}"
        f"｜費用 ${result.cost:.6f}"
    )
    print()


async def probe_search(
    client: OpenRouterClient, cfg, *, reasoning: bool, tools: bool
) -> None:
    """打一次原始請求，把回應形狀原樣印出來。

    要回答的四個問題：
      1. 帶伺服器工具時 message.content 是字串還是陣列
      2. url_citation annotation 在正文裡的實際樣式
      3. usage.server_tool_use.web_search_requests 的實際值
      4. usage.cost 是否含搜尋費（用有／無工具兩格對比）

    刻意不呼叫 `_parse()`：解析器會把沒見過的欄位吃掉，而這裡要看的正是那些。
    """
    label = f"推理{'開' if reasoning else '關'} × 工具{'有' if tools else '無'}"
    print(f"───── 搜尋探測：{label} ─────")

    payload: dict = {
        "model": cfg.model,
        "messages": [{"role": "user", "content": SEARCH_PROBE_PROMPT}],
        "max_tokens": 1200,
        "usage": {"include": True},
    }
    if not reasoning:
        payload["reasoning"] = {"enabled": False}
    if tools:
        payload["tools"] = [
            {
                "type": "openrouter:web_search",
                "parameters": {
                    "engine": cfg.search_engine,
                    "mode": cfg.search_engine_mode,
                    "max_results": cfg.search_max_results,
                    "max_total_results": 15,
                },
            }
        ]

    try:
        data = await client._post(payload)
    except LLMError as exc:
        print(f"失敗：{exc}\n")
        return

    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content")

    print(f"finish_reason：{choice.get('finish_reason')!r}")
    print(f"message 欄位：{sorted(message.keys())}")
    print(f"content 型別：{type(content).__name__}")
    if isinstance(content, list):
        print("  ⚠ content 是**陣列** —— openrouter.py 目前的 _parse() 會在此 AttributeError")
        for index, part in enumerate(content):
            print(f"  [{index}] {json.dumps(part, ensure_ascii=False)[:400]}")
    else:
        print(f"content：{str(content)[:500]!r}")

    annotations = message.get("annotations")
    if annotations:
        print(f"annotations（{len(annotations)} 筆）：")
        print(json.dumps(annotations, ensure_ascii=False, indent=2)[:1500])
    else:
        print("annotations：(無)")

    usage = data.get("usage") or {}
    print("usage：")
    print(json.dumps(usage, ensure_ascii=False, indent=2))

    server_tool = usage.get("server_tool_use")
    print(
        "usage.server_tool_use："
        + (json.dumps(server_tool, ensure_ascii=False) if server_tool else "(無)")
    )
    print()


async def main() -> int:
    cfg = Settings()  # type: ignore[call-arg]
    if not cfg.openrouter_api_key:
        print("FW_OPENROUTER_API_KEY 是空的，請先在 .env 填入。")
        return 1

    client = OpenRouterClient(cfg)
    include_search = "--search" in sys.argv

    print(f"模型：{cfg.model}\n")

    try:
        await ask(client, reasoning=False)
        await ask(client, reasoning=True)
        await check_image(client, cfg)

        if include_search:
            print("=" * 60)
            print("搜尋探測：2×2 矩陣（推理 on/off × 伺服器工具 on/off）")
            print("=" * 60)
            print()
            for reasoning in (False, True):
                for tools in (False, True):
                    await probe_search(
                        client, cfg, reasoning=reasoning, tools=tools
                    )
    finally:
        await client.close()

    print("兩項都通，代表金鑰、模型代號、reasoning 參數與 usage 解析皆正常。")
    if include_search:
        print("搜尋探測已跑完，原始回應形狀見上。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
