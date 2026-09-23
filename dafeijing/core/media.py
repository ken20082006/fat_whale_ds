"""把 Telegram 的圖片與貼圖轉成模型看得懂的格式。

Telegram 傳來的貼圖訊息只有 file_unique_id 與 emoji，沒有圖檔內容，
所以要「看懂」就得自己把檔案抓下來再送給模型。

圖片一律轉成 JPEG 並縮到長邊上限 —— 原圖可能很大，而 token 成本隨尺寸上升，
縮圖對辨識力的損失微乎其微。
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

# 模型的視覺編碼以固定大小的 patch 為單位，實際切法未公開。
# 這個值由實測回推：一張 512x512 的圖約佔 170 token。
# 只用於送出前估算上下文佔用，不用於計費 —— 計費一律看 API 回傳的 usage。
_PATCH = 40


@dataclass(frozen=True)
class PreparedImage:
    data_url: str
    width: int
    height: int
    byte_size: int
    source: str  # photo | sticker

    @property
    def approx_tokens(self) -> int:
        patches = max(1, (self.width // _PATCH) * (self.height // _PATCH))
        return patches + 85  # 加上固定的開頭開銷


class MediaError(RuntimeError):
    """圖片無法處理。訊息可安全顯示給使用者。"""


async def prepare_from_telegram(
    bot,
    file_id: str,
    *,
    max_edge: int,
    source: str,
    attempts: int = 3,
) -> PreparedImage:
    """從 Telegram 下載並轉換。bot 為 telegram.Bot 實例。

    下載逾時很常見（尤其是在家用網路），所以會重試。
    一次失敗就回「拿不到檔案」對使用者來說是無謂的挫折。
    """
    blob: bytes | None = None

    for attempt in range(attempts):
        try:
            tg_file = await bot.get_file(file_id)
            blob = bytes(await tg_file.download_as_bytearray())
            break
        except Exception as exc:
            logger.warning(
                "下載檔案失敗（第 %d/%d 次，%s）：%s", attempt + 1, attempts, file_id, exc
            )
            if attempt + 1 < attempts:
                await asyncio.sleep(1.0 * (attempt + 1))

    if blob is None:
        raise MediaError("本鯨拿不到那個檔案，可能是網路不穩。等一下再傳一次。")

    return prepare_from_bytes(blob, max_edge=max_edge, source=source)


def prepare_from_bytes(blob: bytes, *, max_edge: int, source: str) -> PreparedImage:
    try:
        with Image.open(io.BytesIO(blob)) as opened:
            # 貼圖多為帶透明度的 webp，統一轉成 RGBA 再處理
            image = opened.convert("RGBA")
            if max(image.size) > max_edge:
                image.thumbnail((max_edge, max_edge), Image.LANCZOS)

            # 鋪一層白底。透明背景直接送出去，模型容易把透明處判讀成黑色。
            canvas = Image.new("RGB", image.size, (255, 255, 255))
            canvas.paste(image, mask=image.split()[3])
    except (UnidentifiedImageError, OSError) as exc:
        logger.warning("圖片解碼失敗：%s", exc)
        raise MediaError("這個檔案本鯨讀不出來。") from exc

    buffer = io.BytesIO()
    canvas.save(buffer, format="JPEG", quality=88, optimize=True)
    data = buffer.getvalue()

    return PreparedImage(
        data_url="data:image/jpeg;base64," + base64.b64encode(data).decode("ascii"),
        width=canvas.width,
        height=canvas.height,
        byte_size=len(data),
        source=source,
    )


def describe(message) -> str:
    """為帶圖片的訊息產生一行標註。

    這是給對話紀錄與引用串用的中繼資料，不是給模型看的圖說 ——
    寫成客觀標註而非描述，才不會誘導它去描述畫面。
    """
    if message.sticker:
        # 刻意不寫出 sticker.emoji。那是貼圖包作者標的，不一定對應畫面內容，
        # 餵給模型會讓它照著 emoji 反應而不是照圖。讓它自己看。
        # 但保留格式資訊 —— 動態貼圖送的是第一格縮圖，講清楚它才不會過度解讀。
        kind = "動態貼圖" if message.sticker.is_animated else (
            "影片貼圖" if message.sticker.is_video else "貼圖"
        )
        return f"〔{kind}〕"
    if message.photo:
        return "〔圖片〕"
    return "〔檔案〕"


def pick_file(message) -> tuple[str, str] | None:
    """挑出要下載的檔案，回傳 (file_id, source)。

    動態與影片貼圖無法直接當圖片開，但 Telegram 會附上第一格的靜態縮圖，
    拿它給模型看已經足夠判斷情緒。
    """
    if message.sticker:
        sticker = message.sticker
        if sticker.is_animated or sticker.is_video:
            if sticker.thumbnail is None:
                return None
            return sticker.thumbnail.file_id, "sticker"
        return sticker.file_id, "sticker"

    if message.photo:
        # photo 是各種尺寸的列表，最後一個最大
        return message.photo[-1].file_id, "photo"

    document = message.document
    if document is not None and (document.mime_type or "").startswith("image/"):
        return document.file_id, "photo"

    return None
