"""驗證 OpenRouter 收唔收圖 —— 直接打 chat/completions。

用嚟分清楚：
  (a) 上游（OpenRouter / 模型）根本唔收圖
  (b) Router / dsh / pi-ai 某處冇送到

用法（喺容器內，因為要 OpenRouter key）：
    docker compose exec router python /app/scripts/probe_image.py

唔會改任何嘢，只係打一次 API 然後印結果。成本約 $0.001。
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import urllib.request
import zlib

# 一張 2x2 純色 PNG —— 細到冇可能觸發任何大小限制。
# 如果模型連呢張都講得出顏色，證明條路通；如果話睇唔到，就係上游問題。
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR4nGP8z8DAwMDA"
    "xMDAwMDAwAAAJhAD4f5nWUAAAAAASUVORK5CYII="
)


def main() -> int:
    key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("FW_OPENROUTER_API_KEY")
    if not key:
        print("冇 OPENROUTER_API_KEY", file=sys.stderr)
        return 1

    model = os.environ.get("PROBE_MODEL", "deepseek/deepseek-v4.1-flash-1017")
    b64 = base64.b64encode(PNG).decode()

    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "呢張圖係咩顏色？一個字答。"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }
        ],
        "max_tokens": 50,
        "reasoning": {"enabled": False},
    }

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        print(f"HTTP 失敗：{exc}")
        if hasattr(exc, "read"):
            print(exc.read().decode("utf-8", "replace")[:600])  # type: ignore[attr-defined]
        return 1

    msg = (data.get("choices") or [{}])[0].get("message") or {}
    print("model    :", data.get("model"))
    print("provider :", data.get("provider"))
    print("content  :", repr((msg.get("content") or "")[:300]))
    print("reasoning:", repr((msg.get("reasoning") or "")[:200]))
    usage = data.get("usage") or {}
    print("tokens   :", usage.get("prompt_tokens"), "in /", usage.get("completion_tokens"), "out")
    print("cost     :", usage.get("cost"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
