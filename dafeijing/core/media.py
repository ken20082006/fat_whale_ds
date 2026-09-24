"""把 Telegram 的媒體轉成模型看得懂的格式。

主模型收不下影片與動態貼圖本身，所以換成「幾格畫面」送進去。能取幾格
取決於來源：動畫 GIF 由 Pillow 逐格解，影片由 ffmpeg 抽格，其餘只有一張。

**但抽格只看得到幾個瞬間** —— 連續動作、節奏、字幕變化都看不到。所以
另外有一條路：把整段影片外包給吃得了影片的模型，拿一段文字解說回來
（見 `describe_video`）。外包成功就不必再送圖。

圖片一律轉成 JPEG 並縮到長邊上限 —— 原圖可能很大，而 token 成本隨尺寸上升，
縮圖對辨識力的損失微乎其微。

所有內容一律只在記憶體中處理，唯一例外是影片抽格時必須落的暫存檔，
它在同一個函式內建立與刪除，不跨請求保留任何東西。
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .util import truncate

logger = logging.getLogger(__name__)

# 模型的視覺編碼以固定大小的 patch 為單位，實際切法未公開。
# 這個值由實測回推：一張 512x512 的圖約佔 170 token。
# 只用於送出前估算上下文佔用，不用於計費 —— 計費一律看 API 回傳的 usage。
_PATCH = 40

# 抽單一格的逾時。快 seek 之後正常是毫秒級，這個值只是防止卡死。
FFMPEG_TIMEOUT = 20.0


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


class MediaError(RuntimeError):
    """圖片無法處理。訊息可安全顯示給使用者。"""


# ── 外包看片 ────────────────────────────────────────────

# 抽格只看得到幾個瞬間：做什麼大致看得出，但連續動作、節奏、字幕變化看不到。
# 有些模型直接吃得了整段影片，所以把片丟給它看完，拿一段文字回來 ——
# 之後就當成自己看過的。呼叫端不需要知道這件事。
_DELEGATE_PROMPT = (
    "用一段文字講清楚這段影片在做什麼，講給一個沒看過的人聽。"
    "涵蓋：畫面內容、發生什麼事、出現的人物或物件、有無文字或字幕、整體氣氛。"
    "只描述你真正看到的，不確定就不要講。控制在四句以內，不要前言。"
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


# 這三種都是「一段影片」—— 值得外包去看，而且**不可以只抽幾格充數**。
MOTION_SOURCES = ("video", "video_note", "animation")


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


async def describe_video(bot, picked: "Picked", cfg, llm) -> VideoNote | None:
    """把整段動態素材交給外援模型看，拿回一段解說。

    **失敗一律回 None**，由呼叫端退回抽格 —— 外包是加分項，不是必要路徑。
    所以這裡連例外都不往外丟：外包掛掉不該讓整則訊息讀不到。

    llm 為 None（或未開啟）時直接跳過，呼叫端就不會多付一次呼叫。
    """
    if llm is None or not cfg.video_delegate_enabled:
        return None

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

    data_url = f"data:{sniff_mime(blob)};base64,{base64.b64encode(blob).decode()}"

    try:
        result = await llm.chat(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _DELEGATE_PROMPT},
                        {"type": "video_url", "video_url": {"url": data_url}},
                    ],
                }
            ],
            model=cfg.video_delegate_model,
            max_tokens=cfg.video_delegate_max_tokens,
            # **刻意不傳 reasoning=False。** 這個端點要求一定要推理，傳
            # `{"enabled": false}` 會回 400：「Reasoning is mandatory for this
            # endpoint and cannot be disabled.」不同模型的要求不一樣，所以
            # 這裡交給端點自己決定 —— 傳錯只會白白失敗一次。
        )
    except Exception:
        # 外包用什麼模型、回來什麼形狀都不關這裡的事 —— 任何失敗都等於
        # 「這次沒外包成」，退回抽格就好。
        logger.exception("外包看片失敗，退回抽格")
        return None

    text = (result.text or "").strip()
    if not text:
        return None

    logger.info(
        "外包看片：%s → %d 字（$%.6f）",
        picked.source,
        len(text),
        result.cost,
    )
    return VideoNote(
        text=truncate(text, cfg.video_delegate_max_chars),
        cost=result.cost,
        model=result.model,
    )


# 過大的檔案另外給一句話。「拿不到檔案、可能是網路不穩」對一個太大的檔案
# 是錯誤的診斷 —— 使用者會一直重傳同一個檔案。
TOO_BIG = "這個檔案太大了，本鯨讀不動。傳小一點的，或者截其中一格給我。"


# ── 取得畫面 ────────────────────────────────────────────


async def collect_media(bot, picked: Picked, cfg) -> list[PreparedImage]:
    """取得一則媒體該送進模型的畫面，一張或多張。

    單張、GIF 多格、影片多格三條路都在這裡收斂，呼叫端不必知道差別。
    每一條多格的路失敗都會退回單張 —— 讀少幾格，總比整則訊息讀不到好。
    """
    max_bytes = cfg.image_max_bytes
    frames = cfg.media_frames

    # 真 GIF：Pillow 自己逐格解得開，不必下載縮圖，也不必用到 ffmpeg。
    if picked.source == "animation" and picked.clip_file_id is None:
        if _fits(picked.declared_bytes, max_bytes) and frames > 1:
            blob = await _download(bot, picked.file_id, max_bytes=max_bytes)
            sampled = prepare_gif_frames(
                blob, max_edge=cfg.image_max_edge, source=picked.source, frames=frames
            )
            if sampled:
                return sampled

    # 影片與被轉成 MP4 的動圖：本體在長度與大小上限之內才值得下載來抽格。
    if picked.clip_file_id and frames > 1 and _clip_within_limits(picked, cfg):
        try:
            blob = await _download(bot, picked.clip_file_id, max_bytes=max_bytes)
        except MediaError as exc:
            logger.info("影片本體取不到，退回縮圖：%s", exc)
        else:
            sampled = await prepare_video_frames(
                blob,
                max_edge=cfg.image_max_edge,
                source=picked.source,
                count=frames,
                duration=picked.clip_seconds,
            )
            if sampled:
                return sampled
            logger.info("抽格沒有結果，退回單張：%s", picked.source)

    return [
        await prepare_from_telegram(
            bot,
            picked.file_id,
            max_edge=cfg.image_max_edge,
            source=picked.source,
            max_bytes=max_bytes,
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


def _clip_within_limits(picked: Picked, cfg) -> bool:
    """影片值不值得下載本體來抽格。

    太長或太大的就只取縮圖 —— 完整解碼一段長片會讓回覆延遲到無法接受。
    兩個欄位都可能是 None（Telegram 不一定給），沒給就不擋。
    """
    if cfg.media_max_seconds <= 0:
        return False
    if picked.clip_seconds is not None and picked.clip_seconds > cfg.media_max_seconds:
        logger.info("影片過長（%.0fs），只取一格", picked.clip_seconds)
        return False
    if picked.clip_bytes is not None and picked.clip_bytes > cfg.image_max_bytes:
        logger.info("影片過大（%d bytes），只取一格", picked.clip_bytes)
        return False
    return True


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


def prepare_gif_frames(
    blob: bytes, *, max_edge: int, source: str, frames: int
) -> list[PreparedImage]:
    """從動畫 GIF 逐格取樣。

    Pillow 本身就支援逐格讀取，所以這條路不需要 ffmpeg。
    靜態 GIF（只有一格）就回一張。
    """
    images: list[PreparedImage] = []

    with _decode(blob) as opened:
        total = int(getattr(opened, "n_frames", 1) or 1)
        for index in frame_indexes(total, frames):
            try:
                opened.seek(index)
                images.append(
                    _encode(opened.convert("RGBA"), max_edge=max_edge, source=source)
                )
            except (OSError, EOFError) as exc:
                # 有些 GIF 的格表是壞的，讀到中途才爆。已經拿到的照用，別整批丟掉。
                logger.warning("GIF 第 %d 格讀不出來：%s", index, exc)
                break

    return images


async def prepare_video_frames(
    blob: bytes,
    *,
    max_edge: int,
    source: str,
    count: int,
    duration: float | None,
) -> list[PreparedImage]:
    """用 ffmpeg 從影片抽幾格。

    影片必須先落到暫存檔才能做時間定位 —— 對不可 seek 的 pipe 下 -ss，
    ffmpeg 只能從頭解碼到尾，每抽一格就要重解一次整段片。
    暫存目錄在離開時自動刪除，不跨請求保留任何東西。
    """
    exe = ffmpeg_exe()
    if exe is None:
        logger.info("找不到 ffmpeg，影片只取一格")
        return []

    frames: list[PreparedImage] = []

    with tempfile.TemporaryDirectory(prefix="fatwhale-clip-") as tmp:
        path = Path(tmp) / "clip.bin"
        path.write_bytes(blob)

        for at in sample_offsets(duration, count):
            raw = await _ffmpeg_grab(exe, path, at)
            if raw is None:
                continue
            try:
                frames.append(prepare_from_bytes(raw, max_edge=max_edge, source=source))
            except MediaError:
                continue

    return frames


def ffmpeg_exe() -> str | None:
    """找 ffmpeg 執行檔。

    優先使用 imageio-ffmpeg 自帶的那一份 —— VPS 上不必另裝系統套件。
    退回 PATH 上的 ffmpeg 是為了開發機（通常已經裝了），也讓
    imageio-ffmpeg 沒裝起來時至少還能抽格。
    兩者都沒有就回 None，呼叫端退回單張畫面。
    """
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    return shutil.which("ffmpeg")


async def _ffmpeg_grab(exe: str, path: Path, at: float) -> bytes | None:
    """抓單一格，回傳 PNG 位元組，失敗回 None。

    -ss 放在 -i 之前是關鍵：那是關鍵格快 seek，有檔案時幾乎是即時的。
    放在後面就得從頭解碼到尾，一段三分鐘的片會慢到無法接受。
    """
    proc = await asyncio.create_subprocess_exec(
        exe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{at:.3f}",
        "-i",
        str(path),
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "png",
        "pipe:1",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        out, err = await asyncio.wait_for(proc.communicate(), FFMPEG_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("ffmpeg 抽格逾時（%.3fs）", at)
        return None

    if proc.returncode != 0 or not out:
        logger.warning(
            "ffmpeg 抽格失敗（%.3fs）：%s", at, err.decode("utf-8", "replace")[:200]
        )
        return None
    return out


# ── 取樣位置 ────────────────────────────────────────────


def sample_offsets(duration: float | None, count: int) -> list[float]:
    """影片要抓哪幾個時間點。

    中段均勻分佈，避開首尾 —— 開頭常是標題卡或黑格，結尾常是 logo，
    兩者都看不出內容。長度不明就抓開頭。
    """
    if not duration or duration <= 0:
        return [0.0]
    if count <= 1:
        return [duration / 2]

    start, end = duration * 0.15, duration * 0.85
    if end <= start:
        return [duration / 2]

    step = (end - start) / (count - 1)
    return [start + step * index for index in range(count)]


def frame_indexes(total: int, count: int) -> list[int]:
    """動畫 GIF 要取哪幾格。格數不比要的多的話就全部取。"""
    if count <= 1:
        return [0]
    if total <= count:
        return list(range(total))

    start, end = total * 0.15, total * 0.85
    step = (end - start) / (count - 1)
    picks = {
        min(total - 1, max(0, round(start + step * index))) for index in range(count)
    }
    return sorted(picks)


# ── 標註與挑選 ──────────────────────────────────────────


def describe(message, frames: int | None = None) -> str:
    """為帶媒體的訊息產生一行標註。

    這是給對話紀錄與引用串用的中繼資料，不是給模型看的圖說 ——
    寫成客觀標註而非描述，才不會誘導它去描述畫面。

    frames 是實際送進模型的畫面格數。動態素材一定要講清楚是幾格，
    否則模型會以為自己看過整段。引用串在快取階段只有文字、還不知道格數，
    那時傳 None，不寫出可能錯的數字。
    """
    if message.sticker:
        # 刻意不寫出 sticker.emoji。那是貼圖包作者標的，不一定對應畫面內容，
        # 餵給模型會讓它照著 emoji 反應而不是照圖。讓它自己看。
        kind = "動態貼圖" if message.sticker.is_animated else (
            "影片貼圖" if message.sticker.is_video else "貼圖"
        )
        if message.sticker.is_animated or message.sticker.is_video:
            return _frame_marker(kind, frames)
        return f"〔{kind}〕"

    if message.animation:
        return _frame_marker("動圖", frames)
    if message.video:
        return _frame_marker("影片", frames)
    if message.video_note:
        return _frame_marker("圓形影片", frames)

    if message.photo:
        return "〔圖片〕"
    return "〔檔案〕"


def _frame_marker(kind: str, frames: int | None) -> str:
    """標出這是動態素材，以及模型實際拿到幾格。"""
    if frames is None:
        return f"〔{kind}〕"
    if frames <= 1:
        return f"〔{kind}，只有第一格畫面〕"
    return f"〔{kind}，{frames} 格畫面〕"


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
    return Picked(media.thumbnail.file_id, source, media.thumbnail.file_size)


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
        clip_seconds=media.duration,
    )


def pick_file(message) -> Picked | None:
    """挑出要下載的媒體。

    Telegram 對每一種媒體都附上第一格的靜態縮圖，所以除了真正的 GIF 之外，
    一律以縮圖為底 —— 貼圖是 webp 動畫或 webm，模型都不收，而縮圖已經是 JPEG。
    影片另外帶上本體，長度與大小許可時可以抽多格。
    """
    if message.sticker:
        sticker = message.sticker
        if sticker.is_animated or sticker.is_video:
            return _thumbnail(sticker, "sticker")
        return Picked(sticker.file_id, "sticker", sticker.file_size)

    if message.animation:
        animation = message.animation
        if _animation_is_image(animation):
            # 真 GIF：Pillow 解得開，下載本體逐格抽，解析度比縮圖好得多。
            return Picked(animation.file_id, "animation", animation.file_size)
        # 被轉成 MP4 的動圖：Pillow 開不了，交給 ffmpeg，退路是縮圖。
        return _clip(animation, "animation")

    if message.video:
        return _clip(message.video, "video")

    if message.video_note:
        return _clip(message.video_note, "video_note")

    if message.photo:
        # photo 是各種尺寸的列表，最後一個最大
        largest = message.photo[-1]
        return Picked(largest.file_id, "photo", largest.file_size)

    document = message.document
    if document is not None and (document.mime_type or "").startswith("image/"):
        return Picked(document.file_id, "photo", document.file_size)

    return None
