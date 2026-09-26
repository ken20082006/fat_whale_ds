"""抓取 Telegram 貼圖包，下載並排成一張總表，用來建立對照表。

Telegram 只會在訊息裡給你 file_unique_id 與 emoji，不含圖檔內容，
所以「看懂貼圖」必須先離線把每一張標註好。

輸出：
    assets/stickers/<set>/NN.png        個別貼圖（靜態轉成 PNG）
    assets/stickers/<set>/contact.png  全部排成一張，方便一次檢視
    assets/stickers/<set>/meta.json    中繼資料，含 file_unique_id 與 emoji

**影片貼紙（`.webm`）同動畫貼紙（`.tgs`）**：本身開唔到做圖片，所以改用
Telegram 附嘅**靜態 thumbnail**（`preview_from: "thumbnail"`）。冇呢個
fallback 嘅話成 set 影片貼紙會一張都入唔到 DB —— 見下面 `fetch` 嘅註釋。

用法：
    .venv/Scripts/python scripts/fetch_stickers.py LLM_Moe
    .venv/Scripts/python scripts/fetch_stickers.py LLMG_GIF

⚠️ **跑之前要停咗 Router** —— 大肥鯨個 Router 而家喺容器入面揸住同一個
SQLite。跨 VM（Docker Desktop bind mount）兩邊一齊寫會爛 DB。
    docker compose stop router
    ...跑 script...
    docker compose start router
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path

import httpx
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dafeijing.settings import Settings  # noqa: E402

# ⚠️ 唔加嘅話喺 Windows console（cp950）直接爆 UnicodeEncodeError ——
# 貼圖嘅 emoji 一定唔喺 cp950 入面。實測第一次跑就死喺 print emoji 度。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CELL = 220          # 總表每格邊長
COLUMNS = 5         # 每列幾張
LABEL_HEIGHT = 26   # 每格下方留給編號的空間


async def _download(client, api: str, file_base: str, file_id: str) -> bytes | None:
    """`getFile` 再下載本體。失敗回 None（唔 raise）。"""
    payload = (await client.get(f"{api}/getFile", params={"file_id": file_id})).json()
    if not payload.get("ok"):
        return None
    path = payload["result"]["file_path"]
    return (await client.get(f"{file_base}/{path}")).content


async def main() -> int:
    cfg = Settings()  # type: ignore[call-arg]
    if not cfg.telegram_bot_token:
        print("FW_TELEGRAM_BOT_TOKEN 是空的，請先在 .env 填入。")
        return 1

    set_name = sys.argv[1] if len(sys.argv) > 1 else "LLM_Moe"
    api = f"https://api.telegram.org/bot{cfg.telegram_bot_token}"
    file_base = f"https://api.telegram.org/file/bot{cfg.telegram_bot_token}"

    out_dir = Path("assets/stickers") / set_name
    out_dir.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(timeout=90.0) as client:
        response = await client.get(f"{api}/getStickerSet", params={"name": set_name})
        payload = response.json()
        if not payload.get("ok"):
            print(f"抓取失敗：{payload.get('description')}")
            return 1

        result = payload["result"]
        stickers = result["stickers"]
        print(f"貼圖包：{result.get('title')}（{set_name}）")
        print(f"類型  ：{result.get('sticker_type')}｜共 {len(stickers)} 張\n")

        meta = []
        images: list[tuple[int, Image.Image | None, dict]] = []

        for index, sticker in enumerate(stickers, start=1):
            record = {
                "index": index,
                "file_unique_id": sticker.get("file_unique_id"),
                "file_id": sticker.get("file_id"),
                "emoji": sticker.get("emoji"),
                "is_animated": sticker.get("is_animated"),
                "is_video": sticker.get("is_video"),
                "type": sticker.get("type"),
                "width": sticker.get("width"),
                "height": sticker.get("height"),
            }
            meta.append(record)

            # 動畫（.tgs）同影片（.webm）開唔到做圖片 —— 但 Telegram 為每張
            # 都附一個**靜態 thumbnail**，用佢嚟標註。
            #
            # 冇呢個 fallback 嘅話，成套影片貼紙會一張都入唔到 DB：呢個
            # script 唔會寫 `record["file"]`，而 `label_stickers.py` 只處理
            # 有 `file` 嘅 record。實測 `LLMG_GIF` 成套 59 張都係影片貼紙。
            #
            # ⚠️ 代價：標註只睇到**一格**，GIF 嘅「郁動」睇唔到。對「揀圖用
            # 嘅提示」嚟講夠用（清單每行只顯示 30 字），但笑點喺動作而唔喺
            # 畫面嗰啲會失準。要準就要真係播條 .webm。
            source_id = sticker["file_id"]

            # 影片貼紙（.webm）：連**本體**一齊落。標註會真係播條片，
            # 唔係淨係睇一格 —— GIF 嘅笑點好多時喺郁動度，睇縮圖會標錯。
            # seed-2.0-mini 直接收 webm（`input_modalities` 有 video），
            # 唔使轉檔。
            if sticker.get("is_video"):
                try:
                    raw = await _download(client, api, file_base, sticker["file_id"])
                    if raw is not None:
                        (out_dir / f"{index:02d}.webm").write_bytes(raw)
                        record["video"] = f"{index:02d}.webm"
                except Exception as exc:  # noqa: BLE001
                    record["video_error"] = type(exc).__name__

            if sticker.get("is_animated") or sticker.get("is_video"):
                # 靜態預覽：`.tgs` 同 `.webm` 都開唔到做圖片，但 Telegram
                # 為每張附一個 thumbnail。冇佢嘅話 contact sheet 會空白。
                thumb = sticker.get("thumbnail") or {}
                if not thumb.get("file_id"):
                    record["note"] = "動態貼圖而且冇 thumbnail"
                    images.append((index, None, record))
                    print(f"{index:3d}. {record['emoji'] or '—':4s} 動態且無縮圖，略過")
                    continue
                source_id = thumb["file_id"]
                record["preview_from"] = "thumbnail"

            file_info = await client.get(
                f"{api}/getFile", params={"file_id": source_id}
            )
            file_path = file_info.json()["result"]["file_path"]
            blob = (await client.get(f"{file_base}/{file_path}")).content

            try:
                image = Image.open(io.BytesIO(blob)).convert("RGBA")
            except Exception as exc:  # noqa: BLE001
                # 縮圖格式認唔到就當冇 —— 寧願少一張，好過整批死。
                record["note"] = f"預覽開唔到：{type(exc).__name__}"
                images.append((index, None, record))
                print(f"{index:3d}. {record['emoji'] or '—':4s} 預覽開唔到，略過")
                continue
            image.save(out_dir / f"{index:02d}.png")

            record["file"] = f"{index:02d}.png"
            images.append((index, image, record))
            mark = "（縮圖）" if record.get("preview_from") else ""
            print(f"{index:3d}. {record['emoji'] or '—':4s} {image.width}x{image.height}{mark}")

        # 排成一張總表
        if images:
            rows = (len(images) + COLUMNS - 1) // COLUMNS
            sheet = Image.new(
                "RGB",
                (COLUMNS * CELL, rows * (CELL + LABEL_HEIGHT)),
                (250, 250, 252),
            )
            draw = ImageDraw.Draw(sheet)

            for position, (index, image, _record) in enumerate(images):
                column = position % COLUMNS
                row = position // COLUMNS
                x = column * CELL
                y = row * (CELL + LABEL_HEIGHT)

                draw.rectangle(
                    [x, y, x + CELL - 1, y + CELL + LABEL_HEIGHT - 1],
                    outline=(200, 200, 210),
                )
                if image is not None:
                    thumb = image.copy()
                    thumb.thumbnail((CELL - 16, CELL - 16))
                    sheet.paste(
                        thumb,
                        (x + (CELL - thumb.width) // 2, y + (CELL - thumb.height) // 2),
                        thumb,
                    )
                draw.text((x + 8, y + CELL + 6), f"#{index}", fill=(60, 60, 90))

            sheet.save(out_dir / "contact.png")
            print(f"\n總表：{out_dir / 'contact.png'}")

        (out_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"中繼資料：{out_dir / 'meta.json'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
