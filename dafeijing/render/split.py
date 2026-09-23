"""把長回覆切成 Telegram 能接受的多則訊息。

Telegram 的硬上限是 4096 字元。難點不在長度，而在標籤：
`<b>` 被切一半、或 `</pre>` 落在下一則，整則訊息都會被拒絕。

做法是把 HTML 切成「標籤」與「文字行」兩種單位，逐個累積，
滿了就先把目前開啟的標籤全部關掉再換一則，並於新的一則開頭重新開啟。
所以一則的結尾永遠是合法的，下一則開頭也永遠是合法的。
"""

from __future__ import annotations

import re

TELEGRAM_LIMIT = 4096
_RESERVE = 64  # 保險空間

_TAG_RE = re.compile(r"<[^>]+>")
_TAG_NAME_RE = re.compile(r"</?\s*([a-zA-Z][\w-]*)")
_LINE_RE = re.compile(r"[^\n]*\n|[^\n]+")


def split_html(html_text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    if not html_text:
        return []
    budget = limit - _RESERVE
    if len(html_text) <= budget:
        return [html_text]

    chunks: list[str] = []
    buffer: list[str] = []
    length = 0
    open_tags: list[tuple[str, str]] = []  # (標籤名, 開啟字串)

    def closing() -> str:
        return "".join(f"</{name}>" for name, _ in reversed(open_tags))

    def flush() -> None:
        nonlocal buffer, length
        body = "".join(buffer)
        if body.strip():
            chunks.append(body + closing())
        # 換一則時把還開著的標籤補回開頭，維持巢狀結構
        buffer = [opening for _, opening in open_tags]
        length = sum(len(item) for item in buffer)

    for unit, is_tag in _tokenize(html_text):
        if is_tag:
            name = _tag_name(unit)
            if name:
                if unit.startswith("</"):
                    for index in range(len(open_tags) - 1, -1, -1):
                        if open_tags[index][0] == name:
                            del open_tags[index:]
                            break
                elif not unit.endswith("/>"):
                    open_tags.append((name, unit))

            if length + len(unit) + len(closing()) > budget and buffer:
                flush()
            buffer.append(unit)
            length += len(unit)
            continue

        # 文字單位可能仍然過長（單行超長），先硬切再逐段放入
        for piece in _fit(unit, budget, len(closing())):
            if length + len(piece) + len(closing()) > budget and buffer:
                flush()
            buffer.append(piece)
            length += len(piece)

    if buffer:
        body = "".join(buffer)
        if body.strip():
            chunks.append(body + closing())

    return [chunk for chunk in chunks if chunk.strip()]


def _tokenize(html_text: str) -> list[tuple[str, bool]]:
    """拆成 (內容, 是否標籤) 的序列。標籤不可再分割。"""
    units: list[tuple[str, bool]] = []
    position = 0

    for match in _TAG_RE.finditer(html_text):
        if match.start() > position:
            units.extend((line, False) for line in _lines(html_text[position : match.start()]))
        units.append((match.group(0), True))
        position = match.end()

    if position < len(html_text):
        units.extend((line, False) for line in _lines(html_text[position:]))

    return units


def _lines(text: str) -> list[str]:
    """以換行切開，保留換行符，讓分段盡量落在行邊界。"""
    if not text:
        return []
    return _LINE_RE.findall(text)


def _tag_name(tag: str) -> str | None:
    match = _TAG_NAME_RE.match(tag)
    return match.group(1).lower() if match else None


def _fit(piece: str, budget: int, reserved: int) -> list[str]:
    """單一文字單位若超過可用空間，切成數塊。"""
    allowance = budget - reserved
    if allowance < 200:
        allowance = max(budget // 2, 200)
    if len(piece) <= allowance:
        return [piece]

    out: list[str] = []
    remaining = piece
    while len(remaining) > allowance:
        window = remaining[:allowance]
        cut = window.rfind(" ")
        if cut < allowance // 2:
            cut = allowance
        out.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        out.append(remaining)
    return out
