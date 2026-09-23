"""SQLite 線上備份。

用官方的 backup API，執行中也能安全備份，不會拿到半寫入的檔案。
只用標準函式庫，不需要 sqlite3 CLI。

用法：
    python scripts/backup.py [來源.db] [備份目錄]

排程（crontab，每天凌晨三點）：
    0 3 * * * cd /opt/fatwhale && .venv/bin/python scripts/backup.py >> logs/backup.log 2>&1
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

RETENTION_DAYS = 14


def main() -> int:
    source_path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/fatwhale.db")
    dest_dir = Path(sys.argv[2] if len(sys.argv) > 2 else "backups")

    if not source_path.is_file():
        print(f"找不到資料庫：{source_path}")
        return 1

    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    dest_path = dest_dir / f"fatwhale-{stamp}.db"

    source = sqlite3.connect(source_path)
    target = sqlite3.connect(dest_path)
    try:
        with target:
            source.backup(target)
    finally:
        source.close()
        target.close()

    size_kb = dest_path.stat().st_size / 1024
    print(f"備份完成：{dest_path}（{size_kb:.1f} KB）")

    cutoff = time.time() - RETENTION_DAYS * 86400
    removed = 0
    for old in dest_dir.glob("fatwhale-*.db"):
        if old.stat().st_mtime < cutoff:
            old.unlink()
            removed += 1
    if removed:
        print(f"清除 {removed} 份逾期備份（保留 {RETENTION_DAYS} 天）")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
