"""連結抓取與搜尋意圖偵測。"""

from __future__ import annotations

from dafeijing.core.webfetch import (
    extract,
    find_urls,
    strip_citations,
    wants_search,
)


# ── 來源標註的清理 ──────────────────────────────────────


def test_strips_the_plugin_citation_format():
    """搜尋外掛的預設標註格式是 `(網域 (網址))`。"""
    text = "第一代產品通常伏味最濃 (mashable.com (https://mashable.com/tech/x))。"
    assert strip_citations(text) == "第一代產品通常伏味最濃。"


def test_strips_plain_domain_in_parens():
    assert strip_citations("價錢約 $999 (apple.com)") == "價錢約 $999"
    assert strip_citations("已經落架 (livemint.com/news/y)") == "已經落架"


def test_markdown_link_keeps_its_text():
    """保留連結文字 —— 那可能是模型想表達的內容，只是不該以連結形式出現。"""
    assert strip_citations("參考 [官方文件](https://example.com/doc) 寫的") == (
        "參考 官方文件 寫的"
    )


def test_bare_url_is_left_alone():
    """裸網址通常是對方明確要連結時才出現，不該自動刪。"""
    text = "詳見 https://example.com/bare"
    assert strip_citations(text) == text


def test_plain_text_untouched():
    assert strip_citations("普通回覆，冇任何連結") == "普通回覆，冇任何連結"
    assert strip_citations("") == ""


def test_removes_leftover_whitespace():
    text = "答案在這裡 (a.com (https://a.com/x)) 。"
    cleaned = strip_citations(text)
    assert "  " not in cleaned
    assert " 。" not in cleaned


def test_handles_multiple_citations():
    text = "第一點 (a.com (https://a.com/1))。第二點 (b.com (https://b.com/2))。"
    cleaned = strip_citations(text)
    assert "a.com" not in cleaned
    assert "b.com" not in cleaned
    assert "第一點" in cleaned and "第二點" in cleaned


# ── 搜尋意圖 ────────────────────────────────────────────


def test_detects_explicit_search_requests():
    for phrase in (
        "上網查一下 DeepSeek 最新版本",
        "幫我查台灣今天天氣",
        "搜尋一下這個套件",
        "search for the latest news",
        "google 一下這個錯誤碼",
        "搵下呢個係咩嚟",
        "查下佢係邊個",
        "最新消息係咩",
    ):
        assert wants_search(phrase), phrase


def test_ignores_ordinary_questions():
    for phrase in (
        "你好嗎",
        "幫我寫一個 quicksort",
        "這個函式為什麼會出錯",
        "我今日好攰",
        "解釋一下遞迴",
    ):
        assert not wants_search(phrase), phrase


def test_does_not_fire_on_bare_cha():
    # 單一個「查」字太容易誤中，所以不認。要成組的講法才算。
    assert not wants_search("這個查詢很慢")
    assert not wants_search("查字典")
    assert not wants_search("檢查一下程式碼")


def test_contextual_hints_fire_without_explicit_request():
    """沒明講要查，但答案會隨時間改變。"""
    for phrase in (
        "DeepSeek 最新版本係咩",
        "呢個套件幾時出 2.0",
        "比特幣而家幾錢",
        "台灣今日天氣點",
        "Rust 1.90 有咩新功能",
        "2026 年有咩大事",
        "what is the latest version of httpx",
    ):
        assert wants_search(phrase), phrase


def test_contextual_does_not_fire_on_small_talk():
    """「最近」「現在」這類詞單獨出現很常見，刻意不收，免得每句閒聊都去搜。"""
    for phrase in (
        "我最近很累",
        "我現在不想工作",
        "今日心情唔錯",
        "幫我寫一個 quicksort",
        "解釋一下遞迴",
        "這個函式為什麼會出錯",
    ):
        assert not wants_search(phrase), phrase


def test_mode_off_never_searches():
    assert not wants_search("上網查一下", mode="off")
    assert not wants_search("最新版本係咩", mode="off")


def test_mode_trigger_ignores_contextual():
    assert wants_search("上網查一下", mode="trigger")
    # 情境判斷在 trigger 模式下不生效
    assert not wants_search("DeepSeek 最新版本係咩", mode="trigger")


def test_mode_always_always_searches():
    assert wants_search("你好", mode="always")
    assert wants_search("", mode="always")


def test_bang_wo_cha_does_fire():
    """「幫我查 X」保留為觸發詞。

    它跟「查字典」的界線很模糊，但誤觸發的代價只是多花一次搜尋費，
    漏掉的代價是答錯 —— 兩邊不對稱，所以選擇偏向觸發。
    """
    assert wants_search("幫我查 DeepSeek 的價錢")
    assert wants_search("幫我查字典")


def test_empty_text():
    assert not wants_search("")
    assert not wants_search(None)  # type: ignore[arg-type]


# ── 連結偵測 ────────────────────────────────────────────


def test_finds_urls_in_order():
    text = "看這個 https://a.example/x 還有 http://b.example/y"
    assert find_urls(text) == ["https://a.example/x", "http://b.example/y"]


def test_urls_deduplicated():
    text = "https://a.example 和 https://a.example 一樣"
    assert find_urls(text) == ["https://a.example"]


def test_trailing_punctuation_stripped():
    assert find_urls("參考 https://a.example/x。") == ["https://a.example/x"]
    assert find_urls("see https://a.example/x, then") == ["https://a.example/x"]


def test_urls_inside_brackets():
    assert find_urls("（https://a.example/x）") == ["https://a.example/x"]


def test_no_urls():
    assert find_urls("完全沒有連結") == []


# ── HTML 抽取 ───────────────────────────────────────────


def test_extract_pulls_title_and_text():
    title, text = extract(
        "<html><head><title>標題</title></head>"
        "<body><p>第一段</p><p>第二段</p></body></html>"
    )
    assert title == "標題"
    assert "第一段" in text
    assert "第二段" in text


def test_extract_drops_script_and_style():
    _, text = extract(
        "<body><script>var x = '不該出現';</script>"
        "<style>.a{color:red}</style><p>正文</p></body>"
    )
    assert "正文" in text
    assert "不該出現" not in text
    assert "color:red" not in text


def test_extract_unescapes_entities():
    _, text = extract("<p>a &lt; b &amp;&amp; c &gt; d</p>")
    assert "a < b && c > d" in text


def test_extract_collapses_blank_lines():
    _, text = extract("<p>一</p><p></p><p></p><p></p><p>二</p>")
    assert "\n\n\n" not in text
