from dafeijing.render.split import TELEGRAM_LIMIT, split_html


def test_short_text_single_chunk():
    assert split_html("<b>短</b>") == ["<b>短</b>"]


def test_empty_returns_nothing():
    assert split_html("") == []


def test_long_paragraph_splits_within_limit():
    text = "\n".join(f"第 {i} 行" + "字" * 200 for i in range(100))
    chunks = split_html(text)
    assert len(chunks) > 1
    assert all(len(chunk) <= TELEGRAM_LIMIT for chunk in chunks)


def test_long_single_line_splits_within_limit():
    chunks = split_html("字" * 20000)
    assert len(chunks) > 1
    assert all(len(chunk) <= TELEGRAM_LIMIT for chunk in chunks)


def test_pre_block_is_closed_in_every_chunk():
    html = "<pre><code>" + ("x" * 12000) + "</code></pre>"
    chunks = split_html(html)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.startswith("<pre><code>")
        assert chunk.endswith("</code></pre>")
        assert len(chunk) <= TELEGRAM_LIMIT


def test_pre_block_kept_whole_when_it_fits():
    html = "前言\n\n<pre><code>print(1)</code></pre>\n\n後語"
    assert split_html(html) == [html]


def test_tags_are_not_split_across_chunks():
    text = "<b>" + "字" * 5000 + "</b>"
    chunks = split_html(text)
    for chunk in chunks:
        assert chunk.count("<b>") <= 1
        assert chunk.count("</b>") <= 1
