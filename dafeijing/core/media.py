"""把 Telegram 的媒體轉成模型看得懂的格式。

**圖片**轉成 JPEG 並縮到長邊上限，直接送給主模型 —— 原圖可能很大，而 token
成本隨尺寸上升，縮圖對辨識力的損失微乎其微。

**影片與動圖不走這條路。** 主模型收不下影片本身，而抽幾個定格看不出連續
動作、節奏與字幕變化，還會讓模型以為自己看過整段、講出半真半假的描述。
所以整段片外包給吃得了影片的模型，拿一段文字解說回來（見 `describe_video`）。
外包不成就是沒有這個媒體 —— **不抽格充數**。

所有內容一律只在記憶體中處理，不落任何暫存檔。
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from .util import now_iso, truncate

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
    source: str  # photo | sticker | animation | video | video_note

    @property
    def approx_tokens(self) -> int:
        patches = max(1, (self.width // _PATCH) * (self.height // _PATCH))
        return patches + 85  # 加上固定的開頭開銷


@dataclass(frozen=True)
class Picked:
    """挑出來準備下載的媒體。

    file_id 是「一張靜態畫面」的來源：真 GIF 的本體，或影片與動態貼圖的縮圖。
    declared_bytes 是它的宣告大小，可能為 None —— 有值就能在下載前先擋掉，
    不必先把整個檔案拉下來才知道它太大。

    clip_* 是「可以抽多格的影片本體」，只有影片與被轉成 MP4 的動圖才有。
    它是選配的：長度超過上限、抽格失敗、或根本沒裝 ffmpeg 時，
    都回到 file_id 那一張。
    """

    file_id: str
    source: str
    declared_bytes: int | None = None
    clip_file_id: str | None = None
    clip_bytes: int | None = None
    clip_seconds: float | None = None
    # Telegram 的**穩定**識別碼。file_id 會隨時間輪換，file_unique_id 不會，
    # 所以外包描述的快取要用這一個當鍵 —— 同一條片再傳就重用同一個描述，
    # 既一致又免費。
    unique_id: str | None = None


class MediaError(RuntimeError):
    """圖片無法處理。訊息可安全顯示給使用者。"""


# ── 外包看片 ────────────────────────────────────────────

# 抽格只看得到幾個瞬間：做什麼大致看得出，但連續動作、節奏、字幕變化看不到。
# 有些模型直接吃得了整段影片，所以把片丟給它看完，拿一段文字回來 ——
# 之後就當成自己看過的。呼叫端不需要知道這件事。
_DELEGATE_PROMPT = (
    "用一段文字講清楚這段影片在做什麼，講給一個沒看過的人聽。"
    "要講得具體：畫面內容、發生什麼事、過程與先後次序、出現的人物或物件"
    "（外觀與動作）、鏡頭或節奏的變化、畫面上或字幕出現的文字（照原文寫出來）、"
    "整體氣氛。"
    "只描述你真正看到的，不確定就不要講。不要前言，也不要逐格流水帳。"
)

# data URL 的 mime 不能猜錯，模型會照它解碼。用magic bytes 判，比看來源可靠 ——
# Telegram 的「動圖」可能是 GIF 也可能是被轉過的 MP4。
_GIF_MAGIC = (b"GIF87a", b"GIF89a")
_WEBM_MAGIC = b"\x1a\x45\xdf\xa3"  # EBML，webm / mkv


@dataclass(frozen=True)
class VideoNote:
    """外援模型看完一段動態素材之後的解說。

    `text` 為空而 `skipped` 有值時，代表**刻意沒有外包**（太長或太大）。
    呼叫端應該把那句話講給使用者聽，而不是靜靜退回抽格 —— 否則對方
    會以為整段都被看過了，而實際上我們只看得到幾格。
    """

    text: str = ""
    cost: float = 0.0
    model: str = ""
    skipped: str = ""


# 這幾種都是「一段影片」—— 值得外包去看，而且**不可以只抽幾格充數**。
#
# `sticker_motion` 是影片貼紙（.webm）。它不在 `sticker` 裡面，因為靜態貼圖
# 與動態貼圖（.tgs）都是靜態內容，只有影片貼紙才真的動得起來。
MOTION_SOURCES = ("video", "video_note", "animation", "sticker_motion")


def is_motion(picked: "Picked") -> bool:
    """這個媒體是不是「一段影片」—— 值得外包去看。"""
    return picked.source in MOTION_SOURCES


def is_motion_source(source: str | None) -> bool:
    """同上，但只憑來源字串判斷 —— 引用串只知道來源，沒有 Picked。"""
    return source in MOTION_SOURCES


def sniff_mime(blob: bytes) -> str:
    """從檔頭判斷 mime。"""
    if blob[:6] in _GIF_MAGIC:
        return "image/gif"
    if blob[:4] == _WEBM_MAGIC:
        return "video/webm"
    return "video/mp4"


async def get_cached_note(db, unique_id: str | None) -> str | None:
    """查這條媒體之前外包過的描述。

    **快取不只是省錢，也是為了一致。** 實測同一條 GIF 外包三次得到
    「鯨魚噴水」「掀檯」「街頭窄巷」三個唔同描述 —— 外包模型對短片的
    抽樣不穩定，會自己補。用同一個識別碼鎖住同一個答案。
    """
    if db is None or not unique_id:
        return None
    row = await db.fetchone(
        "SELECT description FROM media_notes WHERE unique_id = ?", (unique_id,)
    )
    return row["description"] if row else None


async def remember_note(
    db, unique_id: str | None, source: str | None, text: str, model: str
) -> None:
    """記住這次外包的結果。同一個 unique_id 再來就重用，不再付費。"""
    if db is None or not unique_id:
        return
    await db.execute(
        "INSERT INTO media_notes (unique_id, source, description, model, created_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(unique_id) DO UPDATE SET description = excluded.description, "
        "model = excluded.model, created_at = excluded.created_at",
        (unique_id, source, text, model, now_iso()),
    )


async def describe_video(bot, picked: "Picked", cfg, llm, db=None) -> VideoNote | None:
    """把整段動態素材交給外援模型看，拿回一段解說。

    **失敗一律回 None**，呼叫端當作「冇睇過呢個媒體」—— 外包是加分項，不是
    必要路徑，所以這裡連例外都不往外丟：外包掛掉不該讓整則訊息讀不到。

    **真 GIF 同影片走唔同模型、用唔同的 content 型別**（見 settings 的
    gif_delegate_model 說明）。分流靠檔頭判斷，不靠來源標籤 —— Telegram 的
    「動圖」可能係 GIF 也可能係被轉過的 MP4，看來源會判錯。

    llm 為 None（或未開啟）時直接跳過，呼叫端就不會多付一次呼叫。
    db 有值時會按 media 的穩定識別碼快取結果 —— 同一條片再傳就重用。
    """
    if llm is None:
        return None

    # 快取要放在大小／長度檢查之前：那是**已經付過錢**的答案，
    # 沒有理由因為省錢的判斷而丟掉它。
    cached = await get_cached_note(db, picked.unique_id)
    if cached:
        logger.info("外包看片：命中快取（%s）", picked.source)
        return VideoNote(text=cached, model="cache")

    # 有本體就用本體；真 GIF 沒有 clip_*，它的 file_id 本身就是 GIF 本體。
    file_id = picked.clip_file_id or picked.file_id
    declared = picked.clip_bytes if picked.clip_file_id else picked.declared_bytes
    limit = cfg.video_delegate_max_bytes

    if declared is not None and declared > limit:
        logger.info("影片過大，不做外包解說：%d bytes", declared)
        return VideoNote(skipped="影片太大，外包不划算")
    # 影片輸入按秒計費，比抽格貴得多。
    if picked.clip_seconds and picked.clip_seconds > cfg.video_delegate_max_seconds:
        logger.info("影片過長，不做外包解說：%.0f 秒", picked.clip_seconds)
        return VideoNote(
            skipped=f"影片長過 {int(cfg.video_delegate_max_seconds)} 秒，外包不划算"
        )

    try:
        blob = await _download(bot, file_id, max_bytes=limit)
    except MediaError as exc:
        logger.info("影片本體取不到，退回抽格：%s", exc)
        return None

    mime = sniff_mime(blob)
    data_url = f"data:{mime};base64,{base64.b64encode(blob).decode()}"

    # **真 GIF 走另一條路：另一個模型，而且要用 image_url。**
    #
    # 這不是偏好問題，是能力問題。實測多個收片模型都「睇唔到」真 GIF，
    # 而且唔會報錯 —— 佢哋會憑空作一個場景，或者回「請提供具體內容」，
    # 而兩種都會被當成正常描述寫入 media_notes 並永久保存。詳見
    # settings.gif_delegate_model。
    if mime.startswith("image/"):
        model = cfg.gif_delegate_model
        media_part = {"type": "image_url", "image_url": {"url": data_url}}
        what = "GIF"
    else:
        model = cfg.video_delegate_model
        media_part = {"type": "video_url", "video_url": {"url": data_url}}
        what = "影片"

    try:
        result = await llm.chat(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _DELEGATE_PROMPT},
                        media_part,
                    ],
                }
            ],
            model=model,
            max_tokens=cfg.video_delegate_max_tokens,
            # **刻意不傳 reasoning=False。** 這個端點要求一定要推理，傳
            # `{"enabled": false}` 會回 400：「Reasoning is mandatory for this
            # endpoint and cannot be disabled.」不同模型的要求不一樣，所以
            # 這裡交給端點自己決定 —— 傳錯只會白白失敗一次。
            #
            # 因為推理是強制的，`video_delegate_max_tokens` 一定要放得闊：
            # 推理 token 會吃掉額度，留太窄會「想」到爆額、正文變空
            # （實測推理佔 780–930 token）。見 settings.py。
        )
    except Exception:
        # 外包用什麼模型、回來什麼形狀都不關這裡的事 —— 任何失敗都等於
        # 「這次沒外包成」，當作冇睇過就好。
        logger.exception("外包看片失敗，當作冇睇過")
        return None

    text = (result.text or "").strip()
    if not text:
        return None

    text = truncate(text, cfg.video_delegate_max_chars)
    await remember_note(db, picked.unique_id, picked.source, text, result.model)

    logger.info(
        "外包%s：%s → %d 字（$%.6f）",
        what,
        picked.source,
        len(text),
        result.cost,
    )
    return VideoNote(text=text, cost=result.cost, model=result.model)


# 過大的檔案另外給一句話。「拿不到檔案、可能是網路不穩」對一個太大的檔案
# 是錯誤的診斷 —— 使用者會一直重傳同一個檔案。
TOO_BIG = "這個檔案太大了，本鯨讀不動。傳小一點的，或者截其中一格給我。"


# ── 取得畫面 ────────────────────────────────────────────


async def collect_media(bot, picked: Picked, cfg) -> list[PreparedImage]:
    """取得一則媒體該送進模型的畫面。

    **只處理靜態媒體。** 影片與動圖一律走 `describe_video` 外包 ——
    抽出來的幾個定格看不出連續動作，卻會讓模型以為自己看過整段，
    然後講出半真半假的描述。
    """
    return [
        await prepare_from_telegram(
            bot,
            picked.file_id,
            max_edge=cfg.image_max_edge,
            source=picked.source,
            max_bytes=cfg.image_max_bytes,
        )
    ]


async def prepare_from_telegram(
    bot,
    file_id: str,
    *,
    max_edge: int,
    source: str,
    max_bytes: int | None = None,
    attempts: int = 3,
) -> PreparedImage:
    """下載單張並轉換。bot 為 telegram.Bot 實例。"""
    blob = await _download(bot, file_id, max_bytes=max_bytes, attempts=attempts)
    return prepare_from_bytes(blob, max_edge=max_edge, source=source)


async def _download(
    bot, file_id: str, *, max_bytes: int | None, attempts: int = 3
) -> bytes:
    """下載檔案本體並驗大小。內容只在記憶體中，不落地。

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

    if max_bytes is not None and len(blob) > max_bytes:
        logger.info("檔案過大：%s（%d bytes）", file_id, len(blob))
        raise MediaError(TOO_BIG)

    return blob


def _fits(declared_bytes: int | None, max_bytes: int) -> bool:
    """Telegram 宣告的大小在不在上限內。沒宣告就當作可以一試。"""
    return declared_bytes is None or declared_bytes <= max_bytes


# ── 解碼與編碼 ──────────────────────────────────────────


def _decode(blob: bytes):
    try:
        return Image.open(io.BytesIO(blob))
    except (UnidentifiedImageError, OSError) as exc:
        logger.warning("圖片解碼失敗：%s", exc)
        raise MediaError("這個檔案本鯨讀不出來。") from exc


def _encode(image, *, max_edge: int, source: str) -> PreparedImage:
    """把一張 PIL 圖統一轉成模型看得懂的 JPEG。"""
    if max(image.size) > max_edge:
        image.thumbnail((max_edge, max_edge), Image.LANCZOS)

    # 鋪一層白底。透明背景直接送出去，模型容易把透明處判讀成黑色。
    canvas = Image.new("RGB", image.size, (255, 255, 255))
    canvas.paste(image, mask=image.split()[3])

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


def prepare_from_bytes(blob: bytes, *, max_edge: int, source: str) -> PreparedImage:
    with _decode(blob) as opened:
        # 貼圖多為帶透明度的 webp，動畫 GIF 是 palette，統一轉成 RGBA 再處理
        return _encode(opened.convert("RGBA"), max_edge=max_edge, source=source)


# ── 標註與挑選 ──────────────────────────────────────────


def describe(message) -> str:
    """為帶媒體的訊息產生一行標註。

    這是給對話紀錄與引用串用的中繼資料，不是給模型看的圖說 ——
    寫成客觀標註而非描述，才不會誘導它去描述畫面。

    **動態素材不寫格數。** 已經沒有抽格這回事了：影片與動圖一律外包，
    內容由 describe_video 的解說負責。
    """
    if message.sticker:
        # 刻意不寫出 sticker.emoji。那是貼圖包作者標的，不一定對應畫面內容，
        # 餵給模型會讓它照著 emoji 反應而不是照圖。讓它自己看。
        kind = "動態貼圖" if message.sticker.is_animated else (
            "影片貼圖" if message.sticker.is_video else "貼圖"
        )
        return f"〔{kind}〕"

    if message.animation:
        return "〔動圖〕"
    if message.video:
        return "〔影片〕"
    if message.video_note:
        return "〔圓形影片〕"

    if message.photo:
        return "〔圖片〕"
    return "〔檔案〕"


def _animation_is_image(animation) -> bool:
    """這個動圖是不是 Pillow 開得了的圖片檔。

    Telegram 的 Animation 定義是「GIF 或無聲 MP4」—— 使用者上傳 GIF 時
    常常被轉成 MP4，那種情況 Pillow 開不了，要交給 ffmpeg。
    """
    return (animation.mime_type or "").startswith("image/")


def _thumbnail(media, source: str) -> Picked | None:
    """只有一張縮圖可用的媒體。沒有縮圖就放棄。"""
    if media.thumbnail is None:
        return None
    return Picked(
        media.thumbnail.file_id,
        source,
        media.thumbnail.file_size,
        unique_id=media.file_unique_id,
    )


def _clip(media, source: str) -> Picked | None:
    """影片型的媒體：一張縮圖，外加一段可以抽格的本體。"""
    if media.thumbnail is None:
        return None
    return Picked(
        file_id=media.thumbnail.file_id,
        source=source,
        declared_bytes=media.thumbnail.file_size,
        clip_file_id=media.file_id,
        clip_bytes=media.file_size,
        # **getattr 而不是直接取。** 這個函式收的是 Video、VideoNote、
        # Animation 與 Sticker 四種；前三種有 duration，**Sticker 沒有**。
        # 直接寫 media.duration 會在影片貼圖上 AttributeError，而
        # cache_group_message 經手群組每一則訊息 —— 一拋錯，全局錯誤
        # 處理器就會在群組回覆，bot 冇被叫都自己出現。
        clip_seconds=getattr(media, "duration", None),
        unique_id=media.file_unique_id,
    )


def pick_file(message) -> Picked | None:
    """挑出要下載的媒體。

    Telegram 對每一種媒體都附上第一格的靜態縮圖，所以除了真正的 GIF 之外，
    一律以縮圖為底 —— 貼圖是 webp 動畫或 webm，模型都不收，而縮圖已經是 JPEG。
    """
    if message.sticker:
        sticker = message.sticker

        # **影片貼紙（.webm）是真的影片** —— 外包看得了，而且貼圖的
        # file_unique_id 永久穩定，所以睇一次就快取住，之後同一張唔使再睇。
        if sticker.is_video:
            return _clip(sticker, "sticker_motion")

        # **動態貼紙（.tgs）是 Lottie JSON，不是影片** —— 外包看唔到，
        # 只有縮圖那張靜態圖可用，所以走一般圖片那條路。
        if sticker.is_animated:
            return _thumbnail(sticker, "sticker")

        return Picked(
            sticker.file_id, "sticker", sticker.file_size,
            unique_id=sticker.file_unique_id,
        )

    if message.animation:
        animation = message.animation
        if _animation_is_image(animation):
            # 真 GIF：Pillow 解得開，下載本體逐格抽，解析度比縮圖好得多。
            return Picked(
                animation.file_id, "animation", animation.file_size,
                unique_id=animation.file_unique_id,
            )
        # 被轉成 MP4 的動圖：Pillow 開不了，交給 ffmpeg，退路是縮圖。
        return _clip(animation, "animation")

    if message.video:
        return _clip(message.video, "video")

    if message.video_note:
        return _clip(message.video_note, "video_note")

    if message.photo:
        # photo 是各種尺寸的列表，最後一個最大
        largest = message.photo[-1]
        return Picked(
            largest.file_id, "photo", largest.file_size,
            unique_id=largest.file_unique_id,
        )

    document = message.document
    if document is not None and (document.mime_type or "").startswith("image/"):
        return Picked(
            document.file_id, "photo", document.file_size,
            unique_id=document.file_unique_id,
        )

    return None
