"""判斷用的 state 組裝。

這是「短追問會唔會被當成要搜尋」嘅關鍵。實際案例：
「iphone duo喎」單獨睇係三個字嘅片段，判斷成 direct（信心 0.81）；
帶上「上一句問緊開賣日期」之後先會判斷成 search。詳見 build_state 的 docstring。
"""

from dafeijing.core.chat import build_state, pick_search


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


# ── pick_search：這一則要怎麼搜 ──────────────────────
#
# 這段出錯過兩次（off 之下 /search 靜默失效、effort 與 route 自相矛盾），
# 所以抽成純函式釘住。


def test_force_search_overrides_even_the_off_mode():
    """/search 是明確要求，off 之下也應該生效。

    舊版寫成 `force and mode != "off"`，於是 off 之下 /search 靜默不查 ——
    使用者以為查了，其實沒有。
    """
    assert pick_search(
        mode="off", force=True, text="查下", route=None, effort=None
    ) == "force"


def test_always_forces_the_plugin_path():
    assert pick_search(
        mode="always", force=False, text="你好", route=None, effort=None
    ) == "force"


def test_off_never_searches():
    assert pick_search(
        mode="off", force=False, text="上網查下", route="search", effort="thorough"
    ) == "off"


def test_trigger_ignores_the_judgment():
    """trigger 刻意只認明講的，所以判斷說要搜也不算。"""
    assert pick_search(
        mode="trigger", force=False, text="你好", route="search", effort=None
    ) == "off"
    assert pick_search(
        mode="trigger", force=False, text="幫我上網查下", route=None, effort=None
    ) == "tool"


def test_thorough_effort_alone_triggers_a_search():
    """判斷說「小眾冷門、撈少會漏」，本身就意味著應該去搜。

    route 與 effort 是分開問的，會出現自相矛盾的組合。真實事故：追問
    「iphone duo呀」，route 說 direct、effort 卻說 thorough —— 結果不搜，
    助理就一路否認那個型號存在，而實際上搜一次就找到（連 apple.com 的
    新聞稿都有）。
    """
    assert pick_search(
        mode="auto",
        force=False,
        text="iphone duo呀",
        route="direct",
        effort="thorough",
    ) == "tool"


def test_route_decides_when_effort_is_not_thorough():
    assert pick_search(
        mode="auto", force=False, text="今日天氣", route="search", effort="quick"
    ) == "tool"
    assert pick_search(
        mode="auto", force=False, text="你好", route="direct", effort="quick"
    ) == "off"


def test_regex_is_a_fallback_not_a_veto():
    """判斷失手時仍然捉得到明講的「上網查」；但 regex 不可以否決判斷。"""
    # 判斷沒回，但對方明講要查
    assert pick_search(
        mode="auto", force=False, text="幫我上網查下", route=None, effort=None
    ) == "tool"
    # 判斷說要搜，即使 regex 不認得這句
    assert pick_search(
        mode="auto", force=False, text="呢個係咩", route="search", effort=None
    ) == "tool"
