"""真 GIF 例外 —— Router 自己睇。

**呢個係唯一 Router 留低嘅媒體工作**，其餘（圖片、影片）交畀 Hermes。

為什麼：Hermes 嘅 `video_analyze` 收唔到真 GIF ——
`_VIDEO_MIME_TYPES` 冇 `.gif`（`vision_tools.py:942-950`），而且佢一律用
`video_url` 送（`_media_messages(prompt, "video_url", ...)`）。

而大肥鯨實測過（`settings.py:240-251`，commit `66a0747`，十個模型）：
真 GIF 經 `video_url` 會 HTTP 400；只有 `xiaomi/mimo-v2.6-flash` 經
`image_url` 睇得到。**其他模型唔會報錯，而係憑空作場景** ——
兩個失敗模式都會寫垃圾入 `media_notes` 永久保存，所以唔可以求其。

分辨方法：`pick_file()` 對真 GIF 回 `source == "animation"` 且**冇**
`clip_file_id`；被 Telegram 轉成 MP4 嘅動圖會有 clip（嗰啲交返 Hermes）。
"""

from __future__ import annotations

import logging

from ..core import media

logger = logging.getLogger(__name__)


def is_real_gif(picked: media.Picked | None) -> bool:
    """係咪真 GIF（Pillow 開得嘅動畫圖片）。

    Telegram 嘅 Animation 係「GIF 或無聲 MP4」—— 使用者上傳 GIF 時
    經常被轉成 MP4，嗰種有 `clip_file_id`，要交返 Hermes。
    """
    if picked is None:
        return False
    return picked.source == "animation" and picked.clip_file_id is None


async def gif_note(bot, message, svc) -> str | None:
    """呢則訊息如果有真 GIF，交去 `gif_delegate_model` 攞一段描述。

    回傳 None 代表唔關事、或者外包唔成 —— **外包唔成就當冇呢個媒體**，
    唔會抽格充數（大肥鯨 `media.py:1-12` 嘅核心設計決定）。
    """
    picked = media.pick_file(message)
    if not is_real_gif(picked):
        return None

    note = await media.describe_video(bot, picked, svc.cfg, svc.llm, db=svc.db)
    if note is None or not note.text:
        if note is not None and note.skipped:
            logger.info("GIF 外包唔成：%s", note.skipped)
        return None
    return note.text.strip()