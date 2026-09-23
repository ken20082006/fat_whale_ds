from dafeijing.render.markdown import strip_markdown, to_telegram_html


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
