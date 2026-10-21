"""用真圖測 OpenRouter —— 分清楚係上游定係 dsh 條路。

預設用群組嗰張（`ba98c661...`），可以用環境變數換：
    PROBE_IMAGE=<attachment id> python scripts/probe_real_image.py

用法（容器內）：
    docker compose exec router python /app/scripts/probe_real_image.py
"""

from __future__ import annotations

import base64
import glob
import json
import os
import sys
import urllib.request

DEFAULT_ID = "ba98c6614e7377ad89c9a537249ca6d4be88b56a77c6969fbcb36e4f8e775966"
ROOT = "/opt/dsh/attachments/v1/objects"


def find(attachment_id: str) -> str:
    prefix = attachment_id[:2]
    path = os.path.join(ROOT, prefix, attachment_id)
    if os.path.exists(path):
        return path
    hits = glob.glob(f"{ROOT}/**/{attachment_id}", recursive=True)
    if hits:
        return hits[0]
    raise SystemExit(f"搵唔到 attachment {attachment_id}")


def main() -> int:
    key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("FW_OPENROUTER_API_KEY")
    if not key:
        print("冇 OPENROUTER_API_KEY", file=sys.stderr)
        return 1

    attachment_id = os.environ.get("PROBE_IMAGE", DEFAULT_ID)
    path = find(attachment_id)
    raw = open(path, "rb").read()
    b64 = base64.b64encode(raw).decode()

    body = {
        "model": os.environ.get("PROBE_MODEL", "deepseek/deepseek-v4.1-flash-1017"),
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "呢張圖有咩？用兩三句講。"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                ],
            }
        ],
        "max_tokens": 200,
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
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        print(f"HTTP 失敗：{exc}")
        if hasattr(exc, "read"):
            print(exc.read().decode("utf-8", "replace")[:800])  # type: ignore[attr-defined]
        return 1

    msg = (data.get("choices") or [{}])[0].get("message") or {}
    print("attachment:", attachment_id)
    print("bytes     :", len(raw))
    print("provider  :", data.get("provider"))
    print("content   :", (msg.get("content") or "")[:500])
    usage = data.get("usage") or {}
    print("tokens    :", usage.get("prompt_tokens"), "in /", usage.get("completion_tokens"), "out")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
