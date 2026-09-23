"""共用小工具。

時間一律以 UTC ISO8601 字串儲存，與 SQLite 的 datetime() 可直接比較。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉 I O 0 1，避免誤讀


def now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now().strftime("%Y-%m-%d %H:%M:%S")


_WEEKDAYS = "一二三四五六日"


def today_text(offset_hours: int, label: str = "") -> str:
    """給模型看的「今天」。

    刻意只到「日」不含時刻：這段文字會進 system prompt，而那是快取前綴。
    帶上分鐘的話每一分鐘都變，快取等於完全失效；只帶日期則一天失效一次。
    """
    tz = timezone(timedelta(hours=offset_hours))
    moment = datetime.now(tz)
    weekday = _WEEKDAYS[moment.weekday()]
    suffix = f"（{label}）" if label else ""
    return f"{moment.strftime('%Y 年 %m 月 %d 日')} 星期{weekday}{suffix}"


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def in_seconds(seconds: int) -> str:
    return iso(now() + timedelta(seconds=seconds))


def in_minutes(minutes: int) -> str:
    return iso(now() + timedelta(minutes=minutes))


def in_days(days: int) -> str:
    return iso(now() + timedelta(days=days))


def humanise_age(iso_ts: str | None) -> str:
    """把時間戳轉成「3 分鐘前」這類描述。"""
    if not iso_ts:
        return "從未"
    try:
        then = datetime.strptime(iso_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return iso_ts
    delta = now() - then
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds} 秒前"
    if seconds < 3600:
        return f"{seconds // 60} 分鐘前"
    if seconds < 86400:
        return f"{seconds // 3600} 小時前"
    return f"{seconds // 86400} 天前"


def mask_name(name: str | None) -> str:
    """移除會干擾 Telegram HTML 解析與 prompt 結構的字元。"""
    if not name:
        return ""
    cleaned = re.sub(r"[\r\n\t]+", " ", name)
    cleaned = re.sub(r"[<>]", "", cleaned)
    return cleaned.strip()[:64]


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))] + suffix
