"""Token 數量估算。

只用於「送出前」決定要不要壓縮歷史，不計費。
真正的用量一律以 API 回應的 usage 欄位為準（見 llm/openrouter.py）。

DeepSeek 的分詞器未公開，這裡用 CJK 逐字、其餘約四字一 token 的近似法。
誤差在可接受範圍，且估算偏保守（寧可早點壓縮）。
"""

from __future__ import annotations

_CJK_RANGES = (
    (0x3040, 0x30FF),   # 日文假名
    (0x3400, 0x4DBF),   # 中日韓擴充 A
    (0x4E00, 0x9FFF),   # 中日韓統一表意文字
    (0xAC00, 0xD7AF),   # 韓文
    (0xF900, 0xFAFF),   # 相容表意文字
)


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    cjk = 0
    for ch in text:
        if _is_cjk(ch):
            cjk += 1
    others = len(text) - cjk
    return cjk + (others + 3) // 4


def estimate_messages(messages: list[dict]) -> int:
    """估算一整組訊息的 token 數，含每則約 4 token 的結構開銷。"""
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            # 多模態：文字部分估算，圖片另行計價
            for part in content:
                if part.get("type") == "text":
                    total += estimate_tokens(part.get("text"))
        total += 4
    return total
