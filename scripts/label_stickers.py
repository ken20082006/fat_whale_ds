"""為已下載的貼圖產生對照表，寫進 stickers 表。

讀圖時可以直接把圖送給模型看，但**挑選**貼圖不行 —— 總不能每次回覆都把
一百多張圖塞進去讓它選。所以要先離線標註一次：每張圖產出「畫面描述」與
「適合使用的時機」，之後回覆時只放一份精簡清單。

已經標註過的不會重跑，可以中斷後續跑。

用法：
    .venv/Scripts/python scripts/label_stickers.py LLM_Moe

## 影片貼圖（`.webm`）

**唔可以淨係睇縮圖** —— GIF 貼圖嘅笑點好多時喺郁動度。所以逐條真係播嚟
標註，用 `VIDEO_MODEL`（`model_utility` 收唔到 video）。

⚠️ **先要轉做 MP4。** Telegram 嘅影片貼紙係 WebM，而 seed-2.0-mini 唔收
（實測 HTTP 400：`The video format matroska,webm is not supported by the API`）。
主機冇 ffmpeg，所以用 Router image 入面嗰個（佢有，係為真 GIF → MP4 而裝）：

    docker run --rm -v "$PWD/assets/stickers/<set>:/w" --entrypoint sh \\
        fat_whale_ds-router -c 'for f in /w/*.webm; do \\
            ffmpeg -y -loglevel error -i "$f" -movflags +faststart \\
                -pix_fmt yuv420p -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" \\
                "${f%.webm}.mp4"; done'

冇 `.mp4` 嘅會跳過（唔會靜靜哋當標咗）。

⚠️ **跑之前要停咗 Router** —— 佢揸住同一個 SQLite，跨 VM 兩邊一齊寫會爛 DB。
    docker compose stop router
    ...跑 script...
    docker compose start router
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dafeijing.core.media import prepare_from_bytes  # noqa: E402
from dafeijing.llm.openrouter import LLMError, OpenRouterClient  # noqa: E402
from dafeijing.settings import Settings  # noqa: E402

# 同 fetch_stickers.py 一樣：貼圖嘅 emoji 唔喺 cp950 入面，唔改就一定爆。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BATCH = 6          # 一次送幾張。太多會讓模型對錯編號，太少則呼叫次數暴增
LABEL_EDGE = 512   # 標註用不著原尺寸，縮小可省 token
_LINE = re.compile(r"^\s*#?(\d+)\s*[|｜]\s*(.+?)\s*[|｜]\s*(.+?)\s*$")
_ONE_LINE = re.compile(r"^\s*(.+?)\s*[|｜]\s*(.+?)\s*$")

# 影片貼圖用嘅模型。**唔可以用 `model_utility`** —— 預設嗰個係
# deepseek-v4.1-flash，`input_modalities` 只有 text/image，收唔到 video。
#
# bytedance-seed/seed-2.0-mini 實測收 webm，而且睇得準（大肥鯨當年四條片
# 都捉到性別、動作、甚至字幕原文）。唔喺 settings 度開一個欄位，係因為
# Router 已經完全唔打媒體 model —— 呢個淨係離線標註用。
VIDEO_MODEL = "bytedance-seed/seed-2.0-mini"

# 影片貼圖逐條打。**唔批次** —— 一個請求塞幾條片，模型好容易對錯編號，
# 而且影片標錯比圖片更難察覺。59 條分開打，每條約 $0.0005。
VIDEO_PROMPT = """\
這是一張 Telegram 貼圖，影片格式，會不斷循環播放。

請輸出一行，格式固定為：
畫面描述|適合使用的情境

規則：
- 畫面描述：一句話講清楚**發生咩事** —— 動作、過程、先後次序，唔止係
  某一格嘅靜態畫面。片上有文字就一併照原文寫出來
- 適合使用的情境：一句話講什麼時候適合發這張
- 只輸出嗰一行，不要前言、不要解釋
- 用半角 | 分隔，不要用其他符號
- **一律使用繁體中文**，連片上的簡體字也要轉成繁體再寫。這些描述之後會進到
  另一個模型的提示裡，用簡體會把它的輸出也帶偏
"""

PROMPT = """\
下面是一組貼圖，每一張前面都標了它的編號（#1、#2 …）。

請為每一張輸出一行，格式固定為：
編號|畫面描述|適合使用的情境

規則：
- 畫面描述：一句話講清楚畫的是什麼、什麼情緒。圖上有文字就一併寫出來
- 適合使用的情境：一句話講什麼時候適合發這張
- 只輸出這些行，不要前言、不要解釋、不要編號以外的文字
- 每行用半角的 | 分隔，不要用其他符號
- **一律使用繁體中文**，連圖上的簡體字也要轉成繁體再寫。這些描述之後會進到
  另一個模型的提示裡，用簡體會把它的輸出也帶偏
"""


async def main() -> int:
    cfg = Settings()  # type: ignore[call-arg]
    if not cfg.openrouter_api_key:
        print("FW_OPENROUTER_API_KEY 是空的，請先在 .env 填入。")
        return 1

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    relabel = "--relabel" in sys.argv

    set_name = args[0] if args else "LLM_Moe"
    pack_dir = Path("assets/stickers") / set_name
    meta_path = pack_dir / "meta.json"

    if not meta_path.is_file():
        print(f"找不到 {meta_path}，先跑 scripts/fetch_stickers.py {set_name}")
        return 1

    records = json.loads(meta_path.read_text(encoding="utf-8"))
    records = [r for r in records if r.get("file")]
    print(f"貼圖包：{set_name}｜可標註 {len(records)} 張")

    conn = sqlite3.connect(cfg.db_path)
    done = set() if relabel else {
        row[0] for row in conn.execute("SELECT file_unique_id FROM stickers")
    }
    todo = [r for r in records if r["file_unique_id"] not in done]
    print(f"已標註 {len(done)} 張，待處理 {len(todo)} 張\n")

    # 已經標註過的只補 file_id —— 舊版本沒有存這一欄，而發送貼圖要用它。
    # 這裡不呼叫模型，純粹是資料修補。
    backfilled = 0
    for record in records:
        if record["file_unique_id"] not in done:
            continue
        cursor = conn.execute(
            "UPDATE stickers SET file_id = ? "
            "WHERE file_unique_id = ? AND (file_id IS NULL OR file_id = '')",
            (record.get("file_id"), record["file_unique_id"]),
        )
        backfilled += cursor.rowcount
    if backfilled:
        conn.commit()
        print(f"補上 {backfilled} 張的 file_id")

    if not todo:
        conn.close()
        print("沒有需要標註的，結束。")
        return 0

    # 影片貼圖（有 .webm 本體）走另一條路：逐條打、另一個模型。
    # 分開係因為 `model_utility` 收唔到 video，而且批次影片會對錯編號。
    video_todo = [r for r in todo if r.get("video")]
    todo = [r for r in todo if not r.get("video")]
    if video_todo:
        print(f"其中 {len(video_todo)} 張係影片貼紙 —— 會逐條播嚟標註\n")

    client = OpenRouterClient(cfg)
    labeled = 0

    try:
        if video_todo:
            labeled += await _label_videos(conn, client, set_name, pack_dir, video_todo)

        for start in range(0, len(todo), BATCH):
            batch = todo[start : start + BATCH]
            content: list[dict] = []

            for record in batch:
                content.append({"type": "text", "text": f"#{record['index']}"})
                image = prepare_from_bytes(
                    (pack_dir / record["file"]).read_bytes(),
                    max_edge=LABEL_EDGE,
                    source="sticker",
                )
                content.append(
                    {"type": "image_url", "image_url": {"url": image.data_url}}
                )

            content.append({"type": "text", "text": PROMPT})

            try:
                result = await client.chat(
                    [{"role": "user", "content": content}],
                    max_tokens=700,
                    reasoning=False,
                )
            except LLMError as exc:
                print(f"  第 {start // BATCH + 1} 批失敗：{exc}")
                continue

            parsed = _parse(result.text)
            written = 0
            for record in batch:
                entry = parsed.get(record["index"])
                if entry is None:
                    continue
                meaning, usage = entry
                conn.execute(
                    "INSERT OR REPLACE INTO stickers "
                    "(file_unique_id, set_name, file_id, emoji, meaning, usage_hint) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        record["file_unique_id"],
                        set_name,
                        record.get("file_id"),
                        record.get("emoji"),
                        meaning,
                        usage,
                    ),
                )
                written += 1

            conn.commit()
            labeled += written
            print(
                f"  第 {start // BATCH + 1} 批：{written}/{len(batch)} 張"
                f"（${result.cost:.6f}）"
            )

    finally:
        await client.close()
        conn.close()

    print(f"\n完成，共標註 {labeled} 張。")
    return 0


async def _label_videos(
    conn, client: OpenRouterClient, set_name: str, pack_dir: Path, records: list[dict]
) -> int:
    """影片貼圖逐條標註 —— **真係播條 `.webm`**，唔係睇一格縮圖。

    為什麼唔用縮圖：GIF 貼圖嘅笑點好多時喺郁動度（彈一下、轉個身、食物
    跌落嚟），一格靜態圖標唔出。大肥鯨當年就係因為咁而交外援模型睇片。

    回傳成功寫入嘅張數。
    """
    written = 0
    total = len(records)

    for position, record in enumerate(records, start=1):
        # ⚠️ **Telegram 嘅影片貼紙係 WebM，而 seed-2.0-mini 唔收 WebM**
        # （實測 400：`The video format matroska,webm is not supported by the API`）。
        # 所以要先轉做 MP4 —— 見檔頭「影片貼圖」一節。
        webm = pack_dir / record["video"]
        mp4 = webm.with_suffix(".mp4")
        path = mp4 if mp4.is_file() else webm
        if path.suffix != ".mp4":
            print(
                f"  [{position}/{total}] #{record['index']} 仲係 .webm —— "
                "要先轉做 mp4（見檔頭），跳過"
            )
            continue

        try:
            blob = path.read_bytes()
        except OSError as exc:
            print(f"  [{position}/{total}] #{record['index']} 讀唔到 {path.name}：{exc}")
            continue

        data_url = "data:video/mp4;base64," + base64.b64encode(blob).decode()
        try:
            result = await client.chat(
                [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": VIDEO_PROMPT},
                            {"type": "video_url", "video_url": {"url": data_url}},
                        ],
                    }
                ],
                model=VIDEO_MODEL,
                max_tokens=700,
                reasoning=False,
            )
        except LLMError as exc:
            print(f"  [{position}/{total}] #{record['index']} 失敗：{exc}")
            continue

        parsed = _parse_one(result.text or "")
        if parsed is None:
            print(f"  [{position}/{total}] #{record['index']} 格式唔啱，跳過")
            continue

        meaning, usage = parsed
        conn.execute(
            "INSERT OR REPLACE INTO stickers "
            "(file_unique_id, set_name, file_id, emoji, meaning, usage_hint) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                record["file_unique_id"],
                set_name,
                record.get("file_id"),
                record.get("emoji"),
                meaning,
                usage,
            ),
        )
        conn.commit()
        written += 1
        print(
            f"  [{position}/{total}] #{record['index']} {record['emoji'] or '—'} "
            f"{usage[:34]}（${result.cost:.6f}）"
        )

    return written


def _parse_one(text: str) -> tuple[str, str] | None:
    """解析單條影片嘅「描述|情境」。攞第一個對得上格式嘅非空行。"""
    for line in text.splitlines():
        match = _ONE_LINE.match(line)
        if match is None:
            continue
        meaning = match.group(1).strip()
        usage = match.group(2).strip()
        if meaning and usage:
            return meaning, usage
    return None


def _parse(text: str) -> dict[int, tuple[str, str]]:
    """把「編號|描述|情境」解析成對照表。格式不對的行直接跳過。"""
    out: dict[int, tuple[str, str]] = {}
    for line in text.splitlines():
        match = _LINE.match(line)
        if match is None:
            continue
        index = int(match.group(1))
        meaning = match.group(2).strip()
        usage = match.group(3).strip()
        if meaning and usage:
            out[index] = (meaning, usage)
    return out


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
