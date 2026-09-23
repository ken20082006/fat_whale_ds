"""抓取使用者貼的連結。

抓回來的內容是**外部文字**，可能藏著誘導性的句子，所以一律當成資料處理，
在提示裡用標記框起來並明講不是指示。

抽取方式是簡易的 HTML 去標籤，不是完整的正文辨識。對文章類頁面夠用，
真的需要更好的效果時再把 extract() 換成 trafilatura 之類的套件即可。
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(r"https?://[^\s<>\"'）)】\]]+")

MAX_URLS = 3               # 一則訊息最多讀幾個連結
MAX_CHARS = 4_000          # 每頁保留多少字，直接影響 token 成本
MAX_BYTES = 2_000_000      # 下載上限，避免拉到大檔案
TIMEOUT = 15.0

# 這些網域抓回來也沒意義，直接跳過
_SKIP_SUFFIXES = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
    ".mp4", ".mp3", ".wav", ".pdf", ".zip", ".rar", ".7z",
)

_DROP_BLOCKS = re.compile(
    r"<(script|style|noscript|nav|header|footer|aside|form|iframe)\b.*?</\1>",
    re.S | re.I,
)
_TAG = re.compile(r"<[^>]+>")
_BLANKS = re.compile(r"\n{3,}")
_SPACES = re.compile(r"[ \t]{2,}")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)

# 偵測「對方在叫我上網查」的意圖。
#
# 用關鍵詞而不是讓模型自己決定要不要呼叫工具 —— 關鍵詞便宜、即時、行為可預測，
# 也不會因為多一輪工具協商而拖慢每次回覆。之後要升級成函式呼叫再說。
#
# 刻意避開單獨一個「查」字（查字典、查錯、查詢餘額都會誤中），
# 只認成組的講法。
_SEARCH_HINTS = re.compile(
    r"("
    r"上網\s*(查|搵|找|搜|看|search)|"
    r"網上\s*(查|搵|找|搜)|"
    r"幫我\s*(查|搵|搜|找)|"
    r"搜尋|搜索|搜一下|搜下|"
    # 「檢查一下」「調查一下」不是在叫我上網，所以前面是這些字就不算
    r"(?<![檢調審])查一下|(?<![檢調審])查下|(?<![檢調審])查一查|(?<![檢調審])查查|"
    r"搵下|搵吓|搵一搵|"
    r"google|谷歌|"
    r"\bsearch\b|\blook\s+up\b|\bfind\s+online\b|\bweb\s+search\b|"
    r"最新(消息|情況|版本|新聞)|即時新聞|最近點"
    r")",
    re.I,
)


@dataclass(frozen=True)
class FetchedPage:
    url: str
    title: str
    text: str

    def as_block(self) -> str:
        """包成提示裡的一段資料。"""
        return f"<網頁 url=\"{self.url}\" title=\"{self.title}\">\n{self.text}\n</網頁>"


def wants_search(text: str) -> bool:
    """對方是不是在叫我上網查。"""
    return bool(_SEARCH_HINTS.search(text or ""))


def find_urls(text: str) -> list[str]:
    """找出訊息裡的連結，去重並保持出現順序。"""
    seen: set[str] = set()
    urls: list[str] = []
    for raw in URL_PATTERN.findall(text or ""):
        # 半角與全角都要剝。中文句子裡的連結後面常直接接句號、逗號或右括號，
        # 不剝掉的話會變成網址的一部分而抓不到。
        cleaned = raw.rstrip(".,;:!?。，、；：！？）】」』")
        if cleaned in seen:
            continue
        seen.add(cleaned)
        urls.append(cleaned)
    return urls


def _is_private_host(host: str) -> bool:
    """擋掉指向內網的位址。

    這是必要的：使用者可以貼 http://192.168.1.1/ 或 localhost，
    讓 bot 代為存取內網服務。伺服器上跑的話這是真的 SSRF 風險。
    """
    if not host:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False  # 解析不了就交給 httpx 去失敗

    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return True
    return False


def _looks_skippable(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(_SKIP_SUFFIXES)


def extract(html_text: str) -> tuple[str, str]:
    """粗略地把 HTML 轉成純文字。回傳 (標題, 內文)。"""
    title_match = _TITLE.search(html_text)
    title = html.unescape(title_match.group(1)).strip() if title_match else ""

    body = _DROP_BLOCKS.sub(" ", html_text)
    body = _TAG.sub("\n", body)
    body = html.unescape(body)
    body = _SPACES.sub(" ", body)
    body = _BLANKS.sub("\n\n", body)

    lines = [line.strip() for line in body.splitlines()]
    text = "\n".join(line for line in lines if line)
    return title[:200], text


async def fetch(client: httpx.AsyncClient, url: str) -> FetchedPage | None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    if _looks_skippable(url):
        logger.debug("略過非網頁連結：%s", url)
        return None
    if _is_private_host(parsed.hostname or ""):
        logger.warning("拒絕抓取內網位址：%s", url)
        return None

    try:
        response = await client.get(
            url,
            follow_redirects=True,
            headers={"User-Agent": "fatwhale-bot/0.1 (+telegram)"},
        )
        response.raise_for_status()
    except Exception as exc:
        logger.warning("抓取失敗（%s）：%s", url, exc)
        return None

    content_type = response.headers.get("content-type", "")
    if "html" not in content_type and "text" not in content_type:
        logger.debug("略過非文字內容（%s）：%s", content_type, url)
        return None

    if len(response.content) > MAX_BYTES:
        logger.warning("頁面過大，只取前段：%s", url)

    title, text = extract(response.text[:MAX_BYTES])
    if len(text) < 80:
        logger.debug("內容過少，略過：%s", url)
        return None

    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n……（內容過長，已截斷）"

    return FetchedPage(url=url, title=title or parsed.netloc, text=text)


async def fetch_all(urls: list[str], limit: int = MAX_URLS) -> list[FetchedPage]:
    """抓取多個連結。單一失敗不影響其他。"""
    targets = urls[:limit]
    if not targets:
        return []

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        pages = await asyncio.gather(*(fetch(client, url) for url in targets))

    return [page for page in pages if page is not None]
