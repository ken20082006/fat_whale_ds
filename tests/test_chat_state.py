"""判斷用的 state 組裝。

這是「短追問會唔會被當成要搜尋」嘅關鍵。實際案例：
「iphone duo喎」單獨睇係三個字嘅片段，判斷成 direct（信心 0.81）；
帶上「上一句問緊開賣日期」之後先會判斷成 search。詳見 build_state 的 docstring。
"""

from dafeijing.core.chat import build_state


def test_state_includes_the_previous_turn():
    recent = [
        {"role": "user", "content": "@bot iphone duo幾時開賣"},
        {
            "role": "assistant",
            "content": "iPhone 17 系列 9 月 19 日開賣，我搜到嘅資料冇 Duo 呢個名。",
        },
    ]
    state = build_state("iphone duo喎", recent)

    # 追問本身與上一輪都要在裡面，否則判斷會失真
    assert "iphone duo喎" in state
    assert "iPhone 17 系列" in state
    assert "使用者" in state and "助理" in state
    # 要講清楚哪句是「現在說的」，否則模型分不清哪句要判斷
    assert "現在說" in state


def test_state_is_just_the_text_when_there_is_no_history():
    assert build_state("你好", []) == "你好"


def test_state_truncates_long_old_turns():
    """上一輪很長時要截短 —— state 是每次判斷都要付的輸入。"""
    long_reply = {"role": "assistant", "content": "字" * 5000}
    state = build_state("ok", [long_reply], limit=100)

    assert len(state) < 400
    assert "ok" in state


def test_state_handles_missing_content():
    """歷史訊息可能沒有 content（例如只有圖）。不該爆。"""
    state = build_state("睇下呢張圖", [{"role": "assistant", "content": None}])
    assert "睇下呢張圖" in state
    assert "None" not in state
