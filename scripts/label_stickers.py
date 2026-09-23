"""為已下載的貼圖產生對照表，寫進 stickers 表。

讀圖時可以直接把圖送給模型看，但**挑選**貼圖不行 —— 總不能每次回覆都把
一百多張圖塞進去讓它選。所以要先離線標註一次：每張圖產出「畫面描述」與
「適合使用的時機」，之後回覆時只放一份精簡清單。

已經標註過的不會重跑，可以中斷後續跑。

用法：
    .venv/Scripts/python scripts/label_stickers.py LLM_Moe
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dafeijing.core.media import prepare_from_bytes  # noqa: E402
from dafeijing.llm.openrouter import LLMError, OpenRouterClient  # noqa: E402
from dafeijing.settings import Settings  # noqa: E402

BATCH = 6          # 一次送幾張。太多會讓模型對錯編號，太少則呼叫次數暴增
LABEL_EDGE = 512   # 標註用不著原尺寸，縮小可省 token
_LINE = re.compile(r"^\s*#?(\d+)\s*[|｜]\s*(.+?)\s*[|｜]\s*(.+?)\s*$")

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

    client = OpenRouterClient(cfg)
    labeled = 0

    try:
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
