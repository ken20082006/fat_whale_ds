"""時間顯示。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from dafeijing.core.util import local_stamp, today_text


def test_today_text_shape():
    text = today_text(8, "香港時間")
    assert re.match(r"^\d{4} 年 \d{2} 月 \d{2} 日 星期[一二三四五六日]（香港時間）$", text), text


def test_today_text_without_label():
    text = today_text(8)
    assert text.endswith("）") is False
    assert "星期" in text


def test_today_text_has_no_time_component():
    """刻意只到「日」。

    這段文字會進 system prompt，也就是快取前綴。帶上時刻的話每一分鐘都變，
    快取等於完全失效。
    """
    text = today_text(8, "香港時間")
    assert ":" not in text
    assert "時" not in text.replace("香港時間", "").replace("小時", "")


def test_today_text_respects_offset():
    """位移不同，日期可能不同。用 UTC 與 +14 比較。"""
    utc = today_text(0)
    kiritimati = today_text(14)

    # 兩者都是合法日期；在 UTC 的下午之後 +14 會跨到隔天
    now_utc = datetime.now(timezone.utc)
    expected = (now_utc + timedelta(hours=14)).strftime("%Y 年 %m 月 %d 日")
    assert kiritimati.startswith(expected)
    assert utc.startswith(now_utc.strftime("%Y 年 %m 月 %d 日"))


def test_weekday_is_correct():
    """對照 Python 自己的 weekday，確認對應表沒有錯位。

    2026-09-23 是星期三。星期一的索引是 0。
    """
    moment = datetime(2026, 9, 23, tzinfo=timezone.utc)
    assert moment.weekday() == 2  # 0=一 1=二 2=三

    weekdays = "一二三四五六日"
    assert weekdays[moment.weekday()] == "三"


# ── local_stamp：發言者標註嘅時間戳 ──────────────────────────────


def test_local_stamp_from_db_string():
    """`group_cache.created_at` 係無 tz 嘅 UTC 字串。

    實測值：2026-09-27 15:45:01 UTC 就係 router DB 嘅 mtime 23:45 HKT。
    """
    assert local_stamp("2026-09-27 15:45:01", 8) == "09-27 23:45"


def test_local_stamp_from_aware_datetime():
    """Telegram 嘅 `message.date` 係 tz-aware UTC。"""
    dt = datetime(2026, 9, 27, 15, 45, 1, tzinfo=timezone.utc)
    assert local_stamp(dt, 8) == "09-27 23:45"


def test_local_stamp_from_naive_datetime_assumes_utc():
    dt = datetime(2026, 9, 27, 15, 45, 1)
    assert local_stamp(dt, 8) == "09-27 23:45"


def test_local_stamp_crosses_midnight():
    """UTC 16:00 → HKT 00:00 第二日。錯咗就會標錯日。"""
    assert local_stamp("2026-09-27 16:00:00", 8) == "09-28 00:00"


def test_local_stamp_respects_offset():
    assert local_stamp("2026-09-27 15:45:01", 0) == "09-27 15:45"
    assert local_stamp("2026-09-27 15:45:01", -5) == "09-27 10:45"


def test_local_stamp_returns_none_when_unknown():
    """寧願唔標時間，好過標個錯嘅 —— callers 當 falsy 處理。"""
    assert local_stamp(None, 8) is None
    assert local_stamp("", 8) is None
    assert local_stamp("唔係時間", 8) is None
    assert local_stamp("2026-09-27", 8) is None  # 冇時刻，格式唔啱
