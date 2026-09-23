"""輸出側的洩漏檢查。

提示層的規則可以被繞過，所以再加一層：回覆若與系統提示的靜態部分出現長時間的
逐字重疊，就當成洩漏擋下來。

只比對靜態部分（人設與規則），**不**比對筆記與摘要 —— 那些是使用者自己的資料，
他問「你記得我什麼」時回答出來是正常的，攔下來反而莫名其妙。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 連續 40 字逐字相同，巧合的可能性極低。太短會誤攔（例如模型覆述一句規則）。
LEAK_WINDOW = 40

LEAK_REPLY = "這個本鯨不能說。換個問題吧。"


def find_system_leak(
    reply: str,
    static_prompt: str,
    window: int = LEAK_WINDOW,
) -> str | None:
    """回傳造成判定的重疊片段，沒有則回傳 None。"""
    if not reply or not static_prompt:
        return None
    if len(reply) < window or len(static_prompt) < window:
        return None

    for start in range(len(reply) - window + 1):
        chunk = reply[start : start + window]
        if chunk in static_prompt:
            return chunk
    return None
