"""Markdown → Telegram HTML。

不用 MarkdownV2：它的轉義規則極易出錯，任何一個沒跳脫的字元就讓整則訊息送不出去。
HTML 模式只需處理 & < > 三個字元，可靠得多。

流程：先把程式碼區塊抽成佔位符（避免其中的符號被當成格式），
再跳脫 HTML，接著轉換行內格式，最後把程式碼還原回去。
"""

from __future__ import annotations

import html
import re

_PLACEHOLDER = "\x00{}\x00"

_FENCE = re.compile(r"```[ \t]*([\w+#.-]*)[ \t]*\r?\n?(.*?)```", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")

_HEADING = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)
_TABLE_SEP = re.compile(r"^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(\|[ \t]*:?-{2,}:?[ \t]*)*\|?[ \t]*$", re.M)
_BULLET = re.compile(r"^([ \t]*)[-*+][ \t]+", re.M)
_BLOCKQUOTE = re.compile(r"^[ \t]{0,3}&gt;[ \t]?(.*)$", re.M)

_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_ITALIC_STAR = re.compile(r"(?<![*\w])\*(?!\*)([^*\n]+?)(?<!\*)\*(?!\*)")
_ITALIC_UNDER = re.compile(r"(?<![\w_])_([^_\n]+?)_(?![\w_])")
_STRIKE = re.compile(r"~~(.+?)~~", re.S)
_SPOILER = re.compile(r"\|\|(.+?)\|\|", re.S)
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")


def to_telegram_html(markdown_text: str) -> str:
    if not markdown_text:
        return ""

    stash: list[str] = []

    def keep(rendered: str) -> str:
        stash.append(rendered)
        return _PLACEHOLDER.format(len(stash) - 1)

    # 1. 程式碼區塊：內容整段保留，不做任何格式轉換
    def _fence(match: re.Match[str]) -> str:
        language = (match.group(1) or "").strip()
        code = match.group(2)
        code = code.rstrip("\n")
        escaped = html.escape(code, quote=False)
        if language:
            return keep(f'<pre><code class="language-{html.escape(language, quote=True)}">'
                        f"{escaped}</code></pre>")
        return keep(f"<pre><code>{escaped}</code></pre>")

    text = _FENCE.sub(_fence, markdown_text)

    # 2. 行內程式碼
    def _inline(match: re.Match[str]) -> str:
        return keep(f"<code>{html.escape(match.group(1), quote=False)}</code>")

    text = _INLINE_CODE.sub(_inline, text)

    # 3. 跳脫 HTML。順序很重要：& 必須最先換。
    text = html.escape(text, quote=False)

    # 4. 區塊層級
    text = _HEADING.sub(lambda m: f"<b>{m.group(2)}</b>", text)
    text = _TABLE_SEP.sub("", text)
    text = _BLOCKQUOTE.sub(lambda m: f"<blockquote>{m.group(1)}</blockquote>", text)
    text = _BULLET.sub(lambda m: f"{m.group(1)}• ", text)

    # 5. 行內格式。粗體必須先於斜體，否則 ** 會被當成兩個 *。
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _STRIKE.sub(r"<s>\1</s>", text)
    text = _SPOILER.sub(r'<span class="tg-spoiler">\1</span>', text)
    text = _ITALIC_STAR.sub(r"<i>\1</i>", text)
    text = _ITALIC_UNDER.sub(r"<i>\1</i>", text)
    text = _LINK.sub(
        lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>', text
    )

    # 6. 還原程式碼
    for index, rendered in enumerate(stash):
        text = text.replace(_PLACEHOLDER.format(index), rendered)

    return text.strip()


def as_expandable_blockquote(text: str) -> str:
    """把純文字包成 Telegram 的「撳一下展開」引用塊。

    用於貼出模型的思考過程。內容**一律跳脫**：思考是任意文字，裡面出現
    `<` 或 `&` 的話，不跳脫就會被 Telegram 當成標籤，整則訊息送不出去。

    空字串回空字串 —— 呼叫端據此決定「唔使送」。
    """
    body = html.escape((text or "").strip(), quote=False)
    if not body:
        return ""
    return f"<blockquote expandable>{body}</blockquote>"


def prepend_thinking(reply_html: str, thinking: str | None) -> str:
    """把思考過程接到回覆**最前面**，成為同一則訊息。

    思考放在上面、答案在下面，與其他 app 的「已思考」收合塊一致。

    **合併而不是另送一則**：一則訊息本來就可以同時放可展開的引用塊與正文，
    分開送會變成兩條訊息，而且群組那條引用鏈還要多照顧一則（見 group.py）。
    長度由 split_html 處理 —— 太長就照常分段，引用塊會自己收尾。
    """
    block = as_expandable_blockquote(thinking or "")
    if not block:
        return reply_html
    return f"{block}\n\n{reply_html}" if reply_html else block


def strip_markdown(text: str) -> str:
    """移除常見 markdown 標記，用於通知等不需要格式的地方。"""
    text = _FENCE.sub(lambda m: m.group(2), text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _BOLD.sub(r"\1", text)
    text = _STRIKE.sub(r"\1", text)
    text = _ITALIC_STAR.sub(r"\1", text)
    return text.strip()
