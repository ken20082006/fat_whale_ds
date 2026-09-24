"""決策模型的純邏輯。

只測純函式 —— 打真實 alpha 端點是 scripts/ 的事，單元測試不該依賴網路。
線上行為要靠實跑驗證（看日誌「判斷 route=… 」那行）。
"""

from dafeijing.llm.decisions import (
    ROUTE_DIRECT,
    ROUTE_REASON,
    ROUTE_SEARCH,
    ROUTE_SEARCH_REASON,
    budget_factor,
    resolve_reasoning,
    route_flags,
    tone_vibe,
)

VIBE_ORDER = ("low", "mid", "high")


# ── resolve_reasoning：三層優先順序 ──────────────────


def test_personal_override_beats_the_router():
    """使用者自己講了就算，不理判斷結果。"""
    assert resolve_reasoning(1, None, fallback=False) is True
    assert resolve_reasoning(0, None, fallback=True) is False

    # 即使路由說「要推理」，/think off 的人也不該被開啟；反之亦然
    assert resolve_reasoning(0, True, fallback=True) is False
    assert resolve_reasoning(1, False, fallback=False) is True


def test_router_decides_when_there_is_no_override():
    assert resolve_reasoning(None, True, fallback=False) is True
    assert resolve_reasoning(None, False, fallback=True) is False


def test_missing_route_falls_back_to_the_global_setting():
    """判斷不可用時退回全域設定 —— 這是它掛掉時的降級路徑。"""
    assert resolve_reasoning(None, None, fallback=True) is True
    assert resolve_reasoning(None, None, fallback=False) is False


# ── route_flags：路由 → 兩個開關 ─────────────────────


def test_route_flags_maps_every_option():
    assert route_flags(ROUTE_DIRECT) == (False, False)
    assert route_flags(ROUTE_REASON) == (True, False)
    assert route_flags(ROUTE_SEARCH) == (False, True)
    assert route_flags(ROUTE_SEARCH_REASON) == (True, True)


def test_unknown_route_gives_no_opinion():
    """認不出來就不要偷偷選一個 —— 後備由呼叫端決定。"""
    assert route_flags(None) == (None, None)
    assert route_flags("亂寫的") == (None, None)


# ── budget_factor：深度 → max_tokens 倍率 ───────────


def test_budget_factor_grows_with_depth():
    low, high = 1.0, 2.0
    assert budget_factor(0.0, low, high) == 1.0
    assert budget_factor(0.5, low, high) == 1.5
    assert budget_factor(1.0, low, high) == 2.0


def test_budget_factor_clamps_out_of_range_values():
    low, high = 1.2, 1.8
    assert budget_factor(-1.0, low, high) == low
    assert budget_factor(9.0, low, high) == high


def test_budget_factor_leaves_the_budget_alone_when_there_is_no_score():
    assert budget_factor(None, 1.2, 1.8) == 1.0


# ── tone_vibe：演出只降不升 ─────────────────────────


def test_a_very_serious_tone_drops_all_the_way_down():
    assert tone_vibe(0.05, "high", VIBE_ORDER) == "low"
    assert tone_vibe(0.05, "mid", VIBE_ORDER) == "low"
    assert tone_vibe(0.05, "low", VIBE_ORDER) == "low"


def test_a_somewhat_serious_tone_drops_one_level():
    assert tone_vibe(0.25, "high", VIBE_ORDER) == "mid"
    # 已經最低就停在最低，不會變成負數
    assert tone_vibe(0.25, "low", VIBE_ORDER) == "low"


def test_a_playful_tone_never_raises_the_intensity():
    """使用者的 /vibe 是上限 —— 逐則判斷只可以收窄，不可以推高。"""
    assert tone_vibe(0.9, "low", VIBE_ORDER) == "low"
    assert tone_vibe(0.9, "mid", VIBE_ORDER) == "mid"
    assert tone_vibe(0.9, "high", VIBE_ORDER) == "high"


def test_missing_or_unknown_values_are_left_alone():
    assert tone_vibe(None, "high", VIBE_ORDER) == "high"
    # 認不出的等級不要亂改，交回原值
    assert tone_vibe(0.05, "亂寫的", VIBE_ORDER) == "亂寫的"
