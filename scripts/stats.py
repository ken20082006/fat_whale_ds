"""用量與費用報告。

Telegram 裡有 /cost 與 /stats，這個是命令列版本 —— 部署在 VPS 上時不必開
Telegram 就能查，也方便接進 cron 做定期報告。

用法：
    .venv/Scripts/python scripts/stats.py [天數]
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dafeijing.settings import Settings  # noqa: E402


def main() -> int:
    cfg = Settings()  # type: ignore[call-arg]
    days = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 1

    if not cfg.db_path.is_file():
        print(f"找不到資料庫：{cfg.db_path}")
        return 1

    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    window = f"-{days} days"

    print(f"模型：{cfg.model}")
    print(f"統計範圍：近 {days} 天\n")

    # ── 總帳 ──
    total = conn.execute(
        "SELECT COUNT(*) AS calls, "
        "COALESCE(SUM(prompt_tokens), 0) AS p, "
        "COALESCE(SUM(completion_tokens), 0) AS c, "
        "COALESCE(SUM(cached_tokens), 0) AS k, "
        "COALESCE(SUM(reasoning_tokens), 0) AS r, "
        "COALESCE(SUM(cost), 0) AS cost "
        "FROM usage_log WHERE created_at >= datetime('now', ?)",
        (window,),
    ).fetchone()

    tokens = total["p"] + total["c"]
    hit_rate = (total["k"] / total["p"] * 100) if total["p"] else 0

    print("【總計】")
    print(f"  呼叫次數    {total['calls']:,}")
    print(f"  輸入 token  {total['p']:,}（快取命中 {total['k']:,}，{hit_rate:.0f}%）")
    print(f"  輸出 token  {total['c']:,}（其中推理 {total['r']:,}）")
    print(f"  總 token    {tokens:,}")
    print(f"  費用        ${total['cost']:.4f}")

    if total["calls"]:
        print(f"  平均每次    ${total['cost'] / total['calls']:.6f}")
    if tokens:
        print(f"  每百萬 token ${total['cost'] / tokens * 1_000_000:.4f}（含搜尋費）")

    # ── 各對話 ──
    rows = conn.execute(
        "SELECT l.chat_id, g.title, COUNT(*) AS calls, "
        "SUM(l.prompt_tokens + l.completion_tokens) AS tokens, "
        "SUM(l.cost) AS cost "
        "FROM usage_log l LEFT JOIN groups g ON g.chat_id = l.chat_id "
        "WHERE l.created_at >= datetime('now', ?) "
        "GROUP BY l.chat_id ORDER BY tokens DESC LIMIT 8",
        (window,),
    ).fetchall()

    if rows:
        print("\n【用量前幾名】")
        for row in rows:
            label = row["title"] or f"chat {row['chat_id']}"
            print(f"  {label[:28]:28s} {row['calls']:>5} 次  {row['tokens']:>8,} token  ${row['cost']:.4f}")

    # ── 記憶現況 ──
    notes = conn.execute("SELECT COUNT(*) AS n FROM memory_notes").fetchone()["n"]
    sessions = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
    messages = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
    cached = conn.execute("SELECT COUNT(*) AS n FROM group_cache").fetchone()["n"]
    stickers = conn.execute(
        "SELECT COUNT(*) AS n FROM stickers WHERE featured = 1 AND file_id IS NOT NULL"
    ).fetchone()["n"]

    print("\n【資料現況】")
    print(f"  長期筆記    {notes} 則")
    print(f"  對話 session {sessions}")
    print(f"  對話原文    {messages} 則")
    print(f"  群組快取    {cached} 則")
    print(f"  精選貼圖    {stickers} 張")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
