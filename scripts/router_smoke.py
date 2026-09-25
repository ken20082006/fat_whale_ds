"""Router 上線前自我檢查。

唔連 Telegram、唔叫模型（除咗最後一項會真係問 Hermes 一句）——
同 `scripts/smoke.py` 同一個精神。

跑法（由 repo 根）：
    python scripts/router_smoke.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dafeijing.router.app import build_application  # noqa: E402
from dafeijing.router.hermes import HermesClient, HermesError  # noqa: E402
from dafeijing.router.services import create_services  # noqa: E402
from dafeijing.settings import Settings  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    print(f"  {mark} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def main() -> int:
    print("Router 自我檢查\n")

    print("[設定]")
    try:
        cfg = Settings()
    except Exception as exc:  # noqa: BLE001
        check("讀取 .env", False, str(exc).splitlines()[0])
        return 1
    check("讀取 .env", True)
    check("FW_TELEGRAM_BOT_TOKEN 已填", bool(cfg.telegram_bot_token))
    check("FW_HERMES_KEY 已填", bool(cfg.hermes_key))
    check("FW_ADMIN_USER_IDS 已填", bool(cfg.admin_ids))
    print(f"    Hermes：{cfg.hermes_url}")
    print(f"    資料庫：{cfg.db_path}")

    print("\n[資料庫]")
    async def init_db() -> bool:
        svc = create_services(cfg)
        await svc.db.connect()
        try:
            tables = await svc.db.fetchall(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            names = {row["name"] for row in tables}
            check("group_cache 存在", "group_cache" in names)
            check("users 存在", "users" in names)
            await svc.hermes.close()
            return True
        finally:
            await svc.db.close()

    try:
        asyncio.run(init_db())
    except Exception as exc:  # noqa: BLE001
        check("連接資料庫", False, str(exc).splitlines()[0])

    print("\n[組裝]")
    try:
        svc = create_services(cfg)
        app = build_application(svc, cfg)
        handlers = sum(len(v) for v in app.handlers.values())
        check("建立 Application", True, f"{handlers} 個 handler")
        check(
            "handler 數目正確",
            handlers == 3,
            "應該係 3：群組快取、群組回應、私聊",
        )
    except Exception as exc:  # noqa: BLE001
        check("建立 Application", False, str(exc).splitlines()[0])

    print("\n[Hermes]")
    if not cfg.hermes_key:
        check("連 Hermes", False, "冇 FW_HERMES_KEY")
    else:
        async def ping() -> None:
            client = HermesClient(cfg.hermes_url, cfg.hermes_key)
            try:
                reply = await client.ask("router-smoke", "答一個字：好")
                check("連 Hermes", True, f"回「{reply.text[:20]}」")
                print(f"    tokens：{reply.input_tokens} in / {reply.output_tokens} out")
            except HermesError as exc:
                check("連 Hermes", False, str(exc)[:120])
            finally:
                await client.close()

        try:
            asyncio.run(ping())
        except Exception as exc:  # noqa: BLE001
            check("連 Hermes", False, str(exc).splitlines()[0])

    print()
    if failures:
        print(f"有 {len(failures)} 項唔通過：" + "、".join(failures))
        return 1
    print("全部通過，可以啟動：python -m dafeijing.router.main")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
