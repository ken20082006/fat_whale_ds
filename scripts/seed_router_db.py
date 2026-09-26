"""將大肥鯨嘅資料搬去 Router 嘅 DB。

Router 用自己一個 DB（`FW_DB_PATH`，預設 `data/router.db`），因為試用期
兩隻 bot 同時跑緊 —— 共用同一個 SQLite 檔會有鎖定風險，而 README:438-453
記錄過一次 `database disk image is malformed` 事故。

但貼圖庫同 per-user 筆記要跟人過去，唔係嘅話 Router 一開波就係空白：
冇貼圖可以送、亦唔記得任何人。

**搬三張表**：

| 表 | 為咩 |
|---|---|
| `stickers` | 精選貼圖清單（`featured=1`）。冇佢就冇貼圖可送 |
| `memory_notes` | per-user 長期記憶。搬咗就即刻記得舊人 |
| `users` | 存取狀態、vibe、思考偏好 |

**唔搬** `group_cache`、`sessions`、`messages` —— 嗰啲係大肥鯨自己嘅
對話狀態，Router 交晒畀 Hermes 管。`runtime_settings`（/tune 覆寫）都唔搬，
兩邊各自調。

用法（由 repo 根目錄）：

    python scripts/seed_router_db.py            # 搬
    python scripts/seed_router_db.py --dry-run  # 只報告，唔寫
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dafeijing.settings import Settings  # noqa: E402

# (表名, 主鍵欄位)。搬之前會清空目標表 —— 呢個 script 係「同步一份過去」，
# 唔係「合併」，所以重複跑係安全嘅。
TABLES: tuple[tuple[str, str], ...] = (
    ("stickers", "rowid"),
    ("memory_notes", "id"),
    ("users", "tg_user_id"),
)

SOURCE = Path("data/fatwhale.db")


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def main() -> int:
    parser = argparse.ArgumentParser(description="將大肥鯨嘅資料搬去 Router")
    parser.add_argument("--dry-run", action="store_true", help="只報告，唔寫入")
    parser.add_argument("--source", default=str(SOURCE), help="來源 DB")
    args = parser.parse_args()

    source = Path(args.source)
    if not source.exists():
        print(f"搵唔到來源：{source}", file=sys.stderr)
        return 1

    cfg = Settings()
    target = Path(cfg.db_path)
    if not target.exists():
        print(
            f"搵唔到目標：{target}\n"
            "　先起一次 Router（`python -m dafeijing.router.main`）等佢建好 schema。",
            file=sys.stderr,
        )
        return 1

    if source.resolve() == target.resolve():
        print("來源同目標係同一個檔 —— 唔使搬。", file=sys.stderr)
        return 1

    print(f"由 {source} 搬去 {target}\n")

    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(target)
    try:
        for table, _pk in TABLES:
            rows = [dict(r) for r in src.execute(f"SELECT * FROM {table}")]
            if not rows:
                print(f"  {table:14} 0 列（跳過）")
                continue

            # 只搬兩邊都有嘅欄位 —— 舊庫可能未補齊新欄，新庫亦可能有
            # 舊庫冇嘅欄（例如 group_cache.conversation 係 Router 加嘅）。
            shared = [c for c in _columns(src, table) if c in _columns(dst, table)]
            placeholders = ", ".join("?" for _ in shared)
            names = ", ".join(shared)

            if args.dry_run:
                print(f"  {table:14} {len(rows):>4} 列會搬（{len(shared)} 欄）")
                continue

            with dst:
                dst.execute(f"DELETE FROM {table}")
                dst.executemany(
                    f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
                    [tuple(row.get(c) for c in shared) for row in rows],
                )
            print(f"  {table:14} {len(rows):>4} 列搬好（{len(shared)} 欄）")
    finally:
        src.close()
        dst.close()

    if args.dry_run:
        print("\n（--dry-run，冇寫入過任何嘢）")
    else:
        print("\n搞掂。Router 下次啟動就會見到。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
