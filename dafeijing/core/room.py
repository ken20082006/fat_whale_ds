"""群組「聊天室模式」嘅兩個開關 ＋ 送訊息嘅進度位標。

**兩個開關係獨立嘅**（用戶定死），逐個群設定：

    開關 A `room_enabled`     成個群共用一條 Hermes 對話（`room:<chat_id>`）
                              關咗 = 原本「每條引用串一條」
    開關 B `read_background`  每次被 @ 附「最近群組對話」做背景
                              關咗 = 淨係見觸發嗰句同引用串

四種組合都行得通。實作上 A 只管**對話名**，B 只管**要唔要附窗口**，
兩者喺 `router/handlers.py` 各自獨立判斷，冇交叉依賴。

`cursor` 係「最後一次成功送去 Hermes 嘅 message_id」。下次只送呢個之後嘅新訊息，
咁 Hermes 條對話就唔會不斷收到重複內容。

⚠️ **`cursor` 以 `conversation` 為 key，唔係 `chat_id`。** 因為開關 A 關咗
嗰陣，同一個群有好多條對話（每條引用串一條），每條都要記住自己睇到邊。

⚠️ **一定要 upsert。** `AccessControl.set_group_allowed()` 嗰種「先確認列存在」
喺呢度用唔到 —— `groups` 表喺 production 根本冇人寫入（`register_group()`
冇 caller），靠佢會靜默失敗。呢度全部 `INSERT … ON CONFLICT DO UPDATE`。
"""

from __future__ import annotations

from ..store.db import Database
from .util import now_iso

# 兩條 upsert 各自喺「新建列」嗰陣畀另一個開關 0，喺「撞列」嗰陣**唔掂**佢。
# 咁兩個開關就唔會互相覆蓋 —— 分開兩條 SQL 而唔係一條，就係為咗呢點。
_SET_ROOM = (
    "INSERT INTO group_room (chat_id, room_enabled, read_background, updated_at) "
    "VALUES (?, ?, 0, ?) "
    "ON CONFLICT(chat_id) DO UPDATE SET "
    "room_enabled = excluded.room_enabled, updated_at = excluded.updated_at"
)

_SET_READ = (
    "INSERT INTO group_room (chat_id, room_enabled, read_background, updated_at) "
    "VALUES (?, 0, ?, ?) "
    "ON CONFLICT(chat_id) DO UPDATE SET "
    "read_background = excluded.read_background, updated_at = excluded.updated_at"
)


class RoomMode:
    """`group_room` / `room_cursor` 兩張表嘅薄存取層。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ── 開關 ────────────────────────────────────────────

    async def status(self, chat_id: int) -> tuple[bool, bool]:
        """回 `(room_enabled, read_background)`。未設定過就 `(False, False)`。

        一次 query 讀齊兩個 —— `/room` 同 `on_group_message` 都要，唔想拆兩次。
        """
        row = await self._db.fetchone(
            "SELECT room_enabled, read_background FROM group_room WHERE chat_id = ?",
            (chat_id,),
        )
        if row is None:
            return False, False
        return bool(row["room_enabled"]), bool(row["read_background"])

    async def room_enabled(self, chat_id: int) -> bool:
        return (await self.status(chat_id))[0]

    async def read_background(self, chat_id: int) -> bool:
        return (await self.status(chat_id))[1]

    async def set_room_enabled(self, chat_id: int, on: bool) -> None:
        """開關 A。**唔會掂 `read_background`。**"""
        await self._db.execute(_SET_ROOM, (chat_id, 1 if on else 0, now_iso()))

    async def set_read_background(self, chat_id: int, on: bool) -> None:
        """開關 B。**唔會掂 `room_enabled`。**"""
        await self._db.execute(_SET_READ, (chat_id, 1 if on else 0, now_iso()))

    # ── cursor ─────────────────────────────────────────

    async def cursor(self, conversation: str) -> int | None:
        """最後一次成功送去 Hermes 嘅 message_id。冇紀錄就 None。

        None 嘅意思係「從來冇送過」—— 呼叫方應該補返最近 N 條做種。
        """
        row = await self._db.fetchone(
            "SELECT cursor_message_id FROM room_cursor WHERE conversation = ?",
            (conversation,),
        )
        return row["cursor_message_id"] if row else None

    async def set_cursor(self, conversation: str, message_id: int) -> None:
        """喺 Hermes **成功回覆之後**先叫呢個。

        失敗就唔好叫 —— 嗰啲訊息 Hermes 根本冇收過，下次重送係正確嘅。
        """
        await self._db.execute(
            "INSERT INTO room_cursor (conversation, cursor_message_id, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(conversation) DO UPDATE SET "
            "cursor_message_id = excluded.cursor_message_id, "
            "updated_at = excluded.updated_at",
            (conversation, message_id, now_iso()),
        )

    async def reset_cursor(self, conversation: str) -> None:
        """`/room reset` —— 刪走位標，下次@就會補返最近 N 條。

        **唔會清 Hermes 嗰邊嘅歷史**（Router 掂唔到）。呢個只係令 Router
        再送一次最近嘅對話做上下文，唔係「重新開一條對話」。
        """
        await self._db.affect(
            "DELETE FROM room_cursor WHERE conversation = ?", (conversation,)
        )
