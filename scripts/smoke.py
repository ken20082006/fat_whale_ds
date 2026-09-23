"""上線前的自我檢查。不連 Telegram、不呼叫模型。

檢查項目：
  · .env 是否齊全
  · 人設檔是否載得進來
  · 資料庫 schema 是否建得起來
  · 所有 handler 是否組裝得起來

用法：
    .venv/Scripts/python scripts/smoke.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dafeijing.bot.app import build_application, create_services  # noqa: E402
from dafeijing.settings import Settings  # noqa: E402
from dafeijing.store.db import Database  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(message)s")

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    print(f"  {mark} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def main() -> int:
    print("大肥鯨自我檢查\n")

    # ── 設定 ──
    print("[設定]")
    try:
        cfg = Settings()  # type: ignore[call-arg]
        check("讀取 .env", True)
    except Exception as exc:
        check("讀取 .env", False, str(exc).splitlines()[0])
        print("\n請先複製 .env.example 為 .env 並填入內容。")
        return 1

    check("FW_TELEGRAM_BOT_TOKEN 已填", bool(cfg.telegram_bot_token), )
    check("FW_OPENROUTER_API_KEY 已填", bool(cfg.openrouter_api_key))
    check(
        "FW_ADMIN_USER_IDS 已填",
        bool(cfg.admin_ids),
        "" if cfg.admin_ids else "沒有管理員就無法核發邀請碼",
    )
    print(f"    模型：{cfg.model}")
    print(f"    邀請碼有效期：{cfg.invite_ttl_seconds} 秒")

    # ── 人設 ──
    print("\n[人設]")
    from dafeijing.core.persona import Persona

    persona = Persona.load(cfg.persona_file)
    check(
        f"載入 {cfg.persona_file}",
        persona.source is not None and persona.source == cfg.persona_file,
        "" if persona.source == cfg.persona_file else f"退回使用 {persona.source}",
    )
    check("人設非空", len(persona.body) > 50, f"{len(persona.body)} 字元")

    # ── 資料庫 ──
    print("\n[資料庫]")

    async def check_db() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "smoke.db")
            try:
                await db.connect()
                tables = await db.fetchall(
                    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
                )
                names = {row["name"] for row in tables}
                expected = {
                    "users",
                    "invites",
                    "sessions",
                    "messages",
                    "memory_notes",
                    "group_cache",
                    "groups",
                    "stickers",
                    "usage_log",
                }
                missing = expected - names
                check("建立 schema", not missing, f"缺少 {missing}" if missing else f"{len(names)} 張表")
            except Exception as exc:
                check("建立 schema", False, str(exc))
            finally:
                await db.close()

    asyncio.run(check_db())

    # ── 組裝 ──
    print("\n[組裝]")
    try:
        services = create_services(cfg)
        application = build_application(services)
        handlers = sum(len(group) for group in application.handlers.values())
        check("建立 Application", True, f"{handlers} 個 handler")
        check("JobQueue 可用", application.job_queue is not None, "定期清理需要它")
    except Exception as exc:
        check("建立 Application", False, str(exc))

    print()
    if FAILURES:
        print(f"有 {len(FAILURES)} 項要處理：{'、'.join(FAILURES)}")
        return 1

    print("全部通過，可以啟動：python -m dafeijing.main")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
