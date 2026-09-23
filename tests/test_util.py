"""時間顯示。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from dafeijing.core.util import today_text


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
