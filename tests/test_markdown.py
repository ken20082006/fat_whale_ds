from dafeijing.render.markdown import (
    as_expandable_blockquote,
    prepend_thinking,
    strip_markdown,
    to_telegram_html,
)


def test_escapes_html():
    assert to_telegram_html("<b>hi</b>") == "&lt;b&gt;hi&lt;/b&gt;"


def test_escapes_ampersand():
    assert to_telegram_html("a & b") == "a &amp; b"


def test_bold_and_italic():
    assert to_telegram_html("**粗**") == "<b>粗</b>"
    assert to_telegram_html("*斜*") == "<i>斜</i>"


def test_bold_wins_over_italic():
    # 若斜體先處理，** 會被拆成兩個 *，這裡就是在守這個順序
    assert to_telegram_html("**粗體**") == "<b>粗體</b>"


def test_strikethrough_and_spoiler():
    assert to_telegram_html("~~刪~~") == "<s>刪</s>"
    assert "tg-spoiler" in to_telegram_html("||雷||")


def test_link():
    assert to_telegram_html("[站](https://example.com)") == (
        '<a href="https://example.com">站</a>'
    )


def test_heading_becomes_bold():
    assert to_telegram_html("## 標題") == "<b>標題</b>"


def test_bullet():
    assert to_telegram_html("- 一\n- 二") == "• 一\n• 二"


def test_code_fence_is_protected():
    html = to_telegram_html("```python\nprint(**x**)\n```")
    assert '<pre><code class="language-python">' in html
    assert "**x**" in html  # 區塊內不做格式轉換
    assert "<b>" not in html


def test_code_fence_content_is_escaped():
    html = to_telegram_html("```\nif a < b && c > d:\n```")
    assert "a &lt; b &amp;&amp; c &gt; d" in html


def test_inline_code_is_protected():
    html = to_telegram_html("用 `a < b` 判斷")
    assert "<code>a &lt; b</code>" in html


def test_no_placeholder_leaks():
    html = to_telegram_html("**粗** 與 `碼` 與 ```\n區塊\n```")
    assert "\x00" not in html


def test_strip_markdown():
    assert strip_markdown("**粗**") == "粗"
    assert strip_markdown("```\ncode\n```") == "code"


# ── 思考過程的呈現（beta）──────────────────────────────


def test_expandable_blockquote_wraps_the_text():
    assert as_expandable_blockquote("想一想") == (
        "<blockquote expandable>想一想</blockquote>"
    )


def test_expandable_blockquote_escapes_html():
    """思考是任意文字 —— 唔跳脫嘅話，裡面一個 `<` 就會令整則訊息送唔出去。"""
    assert as_expandable_blockquote("if a < b && c > d") == (
        "<blockquote expandable>if a &lt; b &amp;&amp; c &gt; d</blockquote>"
    )


def test_expandable_blockquote_keeps_model_tags_as_text():
    """模型自己在思考裡寫的標籤，要當成文字而不是標籤。"""
    assert "<b>" not in as_expandable_blockquote("<b>粗體</b>")


def test_expandable_blockquote_of_nothing_is_empty():
    """空字串回空字串，呼叫端據此決定唔使送。"""
    assert as_expandable_blockquote("") == ""
    assert as_expandable_blockquote("   ") == ""
    assert as_expandable_blockquote(None) == ""


def test_prepend_thinking_puts_it_above_the_answer():
    """思考與答案**同一則訊息** —— 上面思考、下面答案。"""
    assert prepend_thinking("<b>答案</b>", "想一想") == (
        "<blockquote expandable>想一想</blockquote>\n\n<b>答案</b>"
    )


def test_prepend_thinking_without_thinking_leaves_the_reply_untouched():
    """開關關著時，回覆要一個字都唔變 —— beta 不可以影響原有輸出。"""
    assert prepend_thinking("<b>答案</b>", None) == "<b>答案</b>"
    assert prepend_thinking("<b>答案</b>", "   ") == "<b>答案</b>"


def test_prepend_thinking_escapes_but_keeps_the_answer_intact():
    out = prepend_thinking("<i>答</i>", "a < b && c")
    assert out.startswith("<blockquote expandable>a &lt; b &amp;&amp; c</blockquote>")
    assert out.endswith("<i>答</i>")
