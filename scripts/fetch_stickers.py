"""抓取 Telegram 貼圖包，下載並排成一張總表，用來建立對照表。

Telegram 只會在訊息裡給你 file_unique_id 與 emoji，不含圖檔內容，
所以「看懂貼圖」必須先離線把每一張標註好。

輸出：
    assets/stickers/<set>/NN.png        個別貼圖（靜態轉成 PNG）
    assets/stickers/<set>/contact.png  全部排成一張，方便一次檢視
    assets/stickers/<set>/meta.json    中繼資料，含 file_unique_id 與 emoji

用法：
    .venv/Scripts/python scripts/fetch_stickers.py LLM_Moe
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

CELL = 220          # 總表每格邊長
COLUMNS = 5         # 每列幾張
LABEL_HEIGHT = 26   # 每格下方留給編號的空間


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

            # 動畫（.tgs）與影片（.webm）無法直接當圖片開，先只記錄中繼資料
            if sticker.get("is_animated") or sticker.get("is_video"):
                record["note"] = "動態貼圖，未下載預覽"
                images.append((index, None, record))
                print(f"{index:3d}. {record['emoji'] or '—':4s} 動態，略過預覽")
                continue

            file_info = await client.get(
                f"{api}/getFile", params={"file_id": sticker["file_id"]}
            )
            file_path = file_info.json()["result"]["file_path"]
            blob = (await client.get(f"{file_base}/{file_path}")).content

            image = Image.open(io.BytesIO(blob)).convert("RGBA")
            image.save(out_dir / f"{index:02d}.png")

            record["file"] = f"{index:02d}.png"
            images.append((index, image, record))
            print(f"{index:3d}. {record['emoji'] or '—':4s} {image.width}x{image.height}")

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
