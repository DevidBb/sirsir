"""Поиск в интернете и на YouTube через браузер Джарвиса (без API-ключей).

Результаты берутся со страницы поисковика в браузере Джарвиса: DuckDuckGo (HTML-версия),
при неудаче — Bing. YouTube — из данных страницы результатов (``ytInitialData``),
при неудаче — из DOM. Ничего не придумывается: если поисковик показал капчу или
пустую страницу, возвращается честная ошибка.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import asdict, dataclass
from typing import Any, Iterator
from urllib.parse import parse_qs, quote_plus, urlparse

from .browser import BrowserController, BrowserError

log = logging.getLogger(__name__)


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    source: str = ""  # домен

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Video:
    title: str
    url: str
    channel: str = ""
    views: str = ""
    published: str = ""
    duration: str = ""
    description: str = ""
    video_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SearchError(RuntimeError):
    pass


def domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def decode_ddg_href(href: str) -> str:
    """DuckDuckGo оборачивает ссылки в //duckduckgo.com/l/?uddg=<адрес>."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return target
    return href


def decode_bing_href(href: str) -> str:
    """Bing: https://www.bing.com/ck/a?...&u=a1<base64url адреса>."""
    parsed = urlparse(href or "")
    if parsed.netloc.endswith("bing.com") and parsed.path.startswith("/ck/"):
        u = parse_qs(parsed.query).get("u", [""])[0]
        if u.startswith("a1"):
            raw = u[2:]
            try:
                return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return href
    return href


def clean_results(raw: list[dict[str, Any]], decoder, limit: int) -> list[SearchResult]:
    out: list[SearchResult] = []
    seen: set[str] = set()
    for item in raw or []:
        url = decoder(str(item.get("href") or ""))
        title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()
        if not url.startswith(("http://", "https://")) or not title:
            continue
        host = domain(url)
        if host.endswith(("duckduckgo.com", "bing.com")) or url in seen:
            continue  # реклама и служебные ссылки поисковика
        seen.add(url)
        snippet = re.sub(r"\s+", " ", str(item.get("snippet") or "")).strip()[:300]
        out.append(SearchResult(title[:200], url, snippet, host))
        if len(out) >= limit:
            break
    return out


DDG_JS = r"""
() => Array.from(document.querySelectorAll('.result')).filter(r => !r.classList.contains('result--ad'))
  .map(r => {
    const a = r.querySelector('.result__a');
    return {title: a ? a.innerText : '', href: a ? a.getAttribute('href') : '',
            snippet: (r.querySelector('.result__snippet') || {}).innerText || ''};
  })
"""

BING_JS = r"""
() => Array.from(document.querySelectorAll('li.b_algo')).map(r => {
  const a = r.querySelector('h2 a');
  return {title: a ? a.innerText : '', href: a ? a.href : '',
          snippet: (r.querySelector('.b_caption p, .b_lineclamp2, .b_paractl') || {}).innerText || ''};
})
"""

YT_DATA_JS = "() => window.ytInitialData ? window.ytInitialData.contents : null"
YT_DOM_JS = r"""
() => Array.from(document.querySelectorAll('ytd-video-renderer')).map(v => {
  const a = v.querySelector('a#video-title');
  const meta = Array.from(v.querySelectorAll('#metadata-line span')).map(s => s.innerText);
  return {title: a ? (a.getAttribute('title') || a.innerText) : '', href: a ? a.href : '',
          channel: ((v.querySelector('ytd-channel-name a') || {}).innerText || ''),
          views: meta[0] || '', published: meta[1] || '',
          duration: ((v.querySelector('ytd-thumbnail-overlay-time-status-renderer') || {}).innerText || '').trim()};
})
"""

CAPTCHA_MARKERS = ("bots use duckduckgo", "complete the following challenge", "unusual traffic",
                   "are you a robot", "подтвердите, что вы не робот")


def _text(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    if "simpleText" in node:
        return str(node["simpleText"])
    if "content" in node and isinstance(node["content"], str):
        return node["content"]
    return "".join(str(r.get("text", "")) for r in node.get("runs", []) if isinstance(r, dict))


def _walk(node: Any, key: str) -> Iterator[Any]:
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                yield v
            else:
                yield from _walk(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, key)


def parse_youtube(data: Any, limit: int = 5) -> list[Video]:
    """Видео (не shorts, не плейлисты) из ytInitialData.contents страницы поиска."""
    out: list[Video] = []
    seen: set[str] = set()
    for v in _walk(data, "videoRenderer"):
        vid = str(v.get("videoId") or "") if isinstance(v, dict) else ""
        if not vid or vid in seen:
            continue
        seen.add(vid)
        snippet = ""
        for snip in v.get("detailedMetadataSnippets") or []:
            snippet = _text(snip.get("snippetText"))
            break
        snippet = snippet or _text(v.get("descriptionSnippet"))
        out.append(Video(
            title=_text(v.get("title"))[:200],
            url=f"https://www.youtube.com/watch?v={vid}",
            channel=_text(v.get("ownerText") or v.get("longBylineText")),
            views=_text(v.get("viewCountText")).replace("\xa0", " "),
            published=_text(v.get("publishedTimeText")),
            duration=_text(v.get("lengthText")),
            description=snippet.replace("\xa0", " ")[:300],
            video_id=vid))
        if len(out) >= limit:
            break
    return out


def parse_youtube_dom(raw: list[dict[str, Any]], limit: int = 5) -> list[Video]:
    out = []
    for item in raw or []:
        href = str(item.get("href") or "")
        m = re.search(r"[?&]v=([\w-]{6,})", href)
        if not m or not item.get("title"):
            continue
        out.append(Video(title=str(item["title"]).strip()[:200],
                         url=f"https://www.youtube.com/watch?v={m.group(1)}",
                         channel=str(item.get("channel", "")).strip(),
                         views=str(item.get("views", "")).strip(),
                         published=str(item.get("published", "")).strip(),
                         duration=str(item.get("duration", "")).strip(), video_id=m.group(1)))
        if len(out) >= limit:
            break
    return out


# «найди три хороших видео про X» → запрос «X», показать 3.
COUNT_WORDS = {"одно": 1, "два": 2, "две": 2, "пару": 2, "три": 3, "четыре": 4, "пять": 5,
               "шесть": 6, "семь": 7, "восемь": 8, "десять": 10, "несколько": 5}
YT_QUERY_PREFIX = re.compile(
    r"^(?P<count>\d+|" + "|".join(COUNT_WORDS) + r")?\s*"
    r"(?:(?:лучш\w*|хорош\w*|интересн\w*|полезн\w*|топ|top|best|good)\s+)*"
    r"(?:видео|видосы?|видосов|ролики?|роликов|клипы?|videos?)\s+(?:про|о|об|по|на тему|about|on)\s+",
    re.I)


def parse_youtube_query(query: str) -> tuple[str, int | None]:
    """Запрос для YouTube без слов «видео про» и число видео, если оно названо."""
    m = YT_QUERY_PREFIX.match(query.strip())
    if not m:
        return query.strip(), None
    count = m.group("count")
    limit = None
    if count:
        limit = int(count) if count.isdigit() else COUNT_WORDS.get(count.lower())
    rest = query.strip()[m.end():].strip()
    return (rest or query.strip()), limit


def clean_youtube_query(query: str) -> str:
    return parse_youtube_query(query)[0]


class WebSearch:
    """Поиск через браузер. Порядок поисковиков задаётся в config.toml."""

    ENGINES = {
        "duckduckgo": ("https://html.duckduckgo.com/html/?q={q}", DDG_JS, decode_ddg_href),
        "bing": ("https://www.bing.com/search?q={q}&setlang=ru", BING_JS, decode_bing_href),
    }

    def __init__(self, browser: BrowserController, engines: list[str] | None = None):
        self.browser = browser
        self.engines = [e for e in (engines or ["duckduckgo", "bing"]) if e in self.ENGINES]
        if not self.engines:
            self.engines = ["duckduckgo"]

    def search(self, query: str, limit: int = 6) -> tuple[str, list[SearchResult]]:
        errors = []
        for engine in self.engines:
            template, script, decoder = self.ENGINES[engine]
            try:
                info = self.browser.open(template.format(q=quote_plus(query)))
                results = clean_results(self.browser.evaluate(script), decoder, limit)
            except BrowserError as e:
                errors.append(f"{engine}: {e}")
                continue
            if results:
                return engine, results
            low = (info.text or "").lower()
            reason = "показал проверку «вы не робот»" if any(m in low for m in CAPTCHA_MARKERS) \
                else "не вернул результатов"
            errors.append(f"{engine} {reason}")
            log.warning("Поиск %s: %s", engine, reason)
        raise SearchError("; ".join(errors) or "поиск не дал результатов")

    def youtube(self, query: str, limit: int = 5) -> list[Video]:
        url = "https://www.youtube.com/results?search_query=" + quote_plus(query)
        info = self.browser.open(url)
        if "consent." in info.url:
            raise SearchError("YouTube просит принять условия (страница согласия). "
                              "Примите их один раз в окне браузера Джарвиса и повторите.")
        videos = parse_youtube(self.browser.evaluate(YT_DATA_JS), limit)
        if not videos:
            videos = parse_youtube_dom(self.browser.evaluate(YT_DOM_JS), limit)
        if not videos:
            raise SearchError("YouTube не показал видео по этому запросу.")
        return videos
