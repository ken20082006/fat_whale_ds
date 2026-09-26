"""對話命名與發言者標註。

呢兩個函式係 router 嘅核心，刻意寫成**純函式** —— 冇 I/O、冇 Telegram、
冇 Hermes —— 因為佢哋決定咗「邊句嘢入邊條對話」同「邊個講」，錯咗會
靜靜哋將唔同人撈埋一齊，好難查。
"""

from __future__ import annotations

# 對話名的前綴。用嚟分辨同一個 Hermes profile 入面唔同來源嘅對話。
GROUP_PREFIX = "grp"
DM_PREFIX = "dm"

# 顯示名可能會撞到呢兩個字元，佢哋係標註格式嘅分隔符。
# 名有 `]` 嘅話 `[名|id]` 會提早收口，後面全部變成「訊息內容」；
# 名有 `|` 嘅話 id 會讀錯。所以一律換走。
_UNSAFE_IN_NAME = str.maketrans({"[": "(", "]": ")", "|": "/", "\n": " "})


def sanitise_name(name: str | None, user_id: int | str) -> str:
    """將顯示名清理成可以安全放入 `[名|id]` 嘅形式。

    冇名就用 id 代替 —— 寧願難睇，好過估錯人。
    """
    candidate = (name or "").strip()
    if not candidate:
        return str(user_id)
    cleaned = candidate.translate(_UNSAFE_IN_NAME).strip()
    return cleaned or str(user_id)


def attribute(name: str | None, user_id: int | str, text: str) -> str:
    """將一句話標上發言者。

    格式沿用 Hermes 自己觀察群組訊息嗰套（`adapter.py` 嘅
    `_telegram_group_observe_attributed_text`）：

        [陳大文|216587605]
        今日隻船係咪要改期？

    **為什麼一定要標**：一條引用串幾個人講嘢，唔標註嘅話 Hermes 收到
    一串 user 訊息，會當全部係同一個人講 —— 咁「記得我唔食辣」就會
    記落最後嗰個人頭上。
    """
    return f"[{sanitise_name(name, user_id)}|{user_id}]\n{(text or '').strip()}"


def group_conversation(chat_id: int | str, root_message_id: int) -> str:
    """群組對話名：一條引用串一個。

    `root_message_id` 由 `ReplyChain.root_id()` 追出嚟 ——
    冇引用任何訊息嘅話，串根就係訊息自己，即係開新對話。
    """
    return f"{GROUP_PREFIX}:{chat_id}:{root_message_id}"


def dm_conversation(chat_id: int | str, thread_id: int | None = None) -> str:
    """私聊對話名。

    有 topic（BotFather 開咗 Threaded Mode）就跟 topic 分，
    冇就跟 chat 一條。
    """
    if thread_id is None:
        return f"{DM_PREFIX}:{chat_id}"
    return f"{DM_PREFIX}:{chat_id}:{thread_id}"

