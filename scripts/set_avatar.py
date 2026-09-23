"""設定 bot 的頭像。

Bot API 從 9.x 起提供 setMyProfilePhoto，不必手動用 BotFather 上傳。
這個方法收的是 InputProfilePhoto 物件而非單純的檔案，所以要用 attach:// 形式
把檔案掛進去。

用法：
    .venv/Scripts/python scripts/set_avatar.py assets/avatar_512.png
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dafeijing.settings import Settings  # noqa: E402

_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


async def main() -> int:
    if len(sys.argv) < 2:
        print("用法：python scripts/set_avatar.py <圖片路徑>")
        return 1

    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"找不到檔案：{path}")
        return 1

    cfg = Settings()  # type: ignore[call-arg]
    if not cfg.telegram_bot_token:
        # token 為空時 Telegram 會回 404，訊息完全指不出原因，這裡先擋下來
        print("FW_TELEGRAM_BOT_TOKEN 是空的，請先在 .env 填入。")
        return 1

    url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/setMyProfilePhoto"
    mime = _MIME.get(path.suffix.lower(), "image/png")
    blob = path.read_bytes()

    print(f"圖片：{path}（{len(blob) / 1024:.1f} KB, {mime}）")

    # 兩種送法都試。Telegram 對這個方法的參數形狀要求較嚴，
    # 直接送檔案在某些版本會回 "photo isn't specified"。
    attempts = (
        (
            "attach:// 形式",
            {"photo": '{"type":"static","photo":"attach://file"}'},
            {"file": (path.name, blob, mime)},
        ),
        (
            "直接上傳",
            {},
            {"photo": (path.name, blob, mime)},
        ),
    )

    async with httpx.AsyncClient(timeout=60.0) as client:
        for label, data, files in attempts:
            response = await client.post(url, data=data, files=files)
            payload = response.json()
            if payload.get("ok"):
                print(f"{label}：成功")
                me = await client.get(
                    f"https://api.telegram.org/bot{cfg.telegram_bot_token}/getMe"
                )
                result = me.json().get("result", {})
                print(f"  @{result.get('username')}（{result.get('first_name')}）")
                return 0
            print(f"{label}：失敗 — {payload.get('description')}")

    print("\n兩種送法都不成功。改用 BotFather 的 /setuserpic 手動上傳。")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
