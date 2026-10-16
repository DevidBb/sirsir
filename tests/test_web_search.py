import json
from base64 import urlsafe_b64encode
from pathlib import Path

import pytest

from jarvis.browser import BrowserError, check_url, is_risky_name
from jarvis.web_search import (WebSearch, SearchError, clean_results, decode_bing_href, decode_ddg_href,
                               parse_youtube, parse_youtube_dom, parse_youtube_query)

FIXTURE = Path(__file__).parent / "fixtures" / "youtube_search.json"


def test_parse_youtube_real_page_data():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    videos = parse_youtube(data, limit=10)
    # 3 разных видео; повтор и shorts пропущены
    assert [v.video_id for v in videos] == ["XvbVePuP7NY", "x2k-UTsfAeA", "78JFdQd04Kg"]
    first = videos[0]
    assert first.url == "https://www.youtube.com/watch?v=XvbVePuP7NY"
    assert first.title and first.channel == "NetworkChuck"
    assert first.duration == "39:41"
    assert "просмотр" in first.views and "\xa0" not in first.views


def test_parse_youtube_limit_and_garbage():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert len(parse_youtube(data, limit=2)) == 2
    assert parse_youtube(None) == []
    assert parse_youtube({"videoRenderer": {"title": "без id"}}) == []


def test_parse_youtube_dom_fallback():
    raw = [{"title": "Видео", "href": "https://www.youtube.com/watch?v=abcdef12345&t=1", "channel": " Канал "},
           {"title": "Шорт", "href": "https://www.youtube.com/shorts/xyz"}]
    videos = parse_youtube_dom(raw)
    assert len(videos) == 1 and videos[0].video_id == "abcdef12345" and videos[0].channel == "Канал"


def test_decode_ddg_and_bing_links():
    assert decode_ddg_href("//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%3Fb%3D1&rut=x") == \
        "https://example.com/a?b=1"
    assert decode_ddg_href("https://example.org/") == "https://example.org/"
    enc = urlsafe_b64encode(b"https://site.ru/page").decode().rstrip("=")
    assert decode_bing_href(f"https://www.bing.com/ck/a?!&&p=1&u=a1{enc}&ntb=1") == "https://site.ru/page"


def test_clean_results_drops_ads_duplicates_and_non_http():
    raw = [{"title": "A", "href": "https://a.com/", "snippet": "  текст  "},
           {"title": "A again", "href": "https://a.com/"},
           {"title": "Ad", "href": "https://duckduckgo.com/y.js?ad"},
           {"title": "js", "href": "javascript:alert(1)"},
           {"title": "", "href": "https://b.com/"},
           {"title": "B", "href": "https://www.b.com/x"}]
    res = clean_results(raw, lambda h: h, limit=5)
    assert [(r.title, r.source) for r in res] == [("A", "a.com"), ("B", "b.com")]
    assert res[0].snippet == "текст"


@pytest.mark.parametrize("query,expected", [
    ("три хороших видео про создание голосового помощника", ("создание голосового помощника", 3)),
    ("видео про локальных ии ассистентов", ("локальных ии ассистентов", None)),
    ("lofi beats", ("lofi beats", None)),
    ("3 best videos about rust", ("rust", 3)),
])
def test_parse_youtube_query(query, expected):
    assert parse_youtube_query(query) == expected


def test_check_url_allows_only_http():
    assert check_url("habr.com") == "https://habr.com"
    assert check_url("http://127.0.0.1:8000/x") == "http://127.0.0.1:8000/x"
    for bad in ("file:///etc/passwd", "javascript:alert(1)", "chrome://settings", ""):
        with pytest.raises(BrowserError):
            check_url(bad)


def test_risky_names():
    for name in ("Купить", "Оформить заказ", "Отправить", "Delete account", "Place order", "Pay now"):
        assert is_risky_name(name), name
    for name in ("Поиск", "Search", "Далее", "Читать статью", "Next page"):
        assert not is_risky_name(name), name


class FakeInfo:
    def __init__(self, url="https://x", text=""):
        self.url, self.text, self.title = url, text, ""


class FakeBrowser:
    def __init__(self, results_by_host, text=""):
        self.results_by_host = results_by_host
        self.text = text
        self.opened = []

    def open(self, url, new_tab=False):
        self.opened.append(url)
        self.current = url
        return FakeInfo(url, self.text)

    def evaluate(self, script, arg=None):
        for host, results in self.results_by_host.items():
            if host in self.current:
                if isinstance(results, Exception):
                    raise results
                return results
        return []


def test_search_falls_back_to_next_engine():
    browser = FakeBrowser({"duckduckgo": [], "bing": [{"title": "R", "href": "https://r.com/"}]})
    engine, results = WebSearch(browser).search("запрос с пробелами")
    assert engine == "bing" and results[0].url == "https://r.com/"
    assert "q=%D0%B7%D0%B0%D0%BF%D1%80%D0%BE%D1%81+%D1%81" in browser.opened[0]


def test_search_reports_captcha_honestly():
    browser = FakeBrowser({}, text="Unfortunately, bots use DuckDuckGo too.")
    with pytest.raises(SearchError) as e:
        WebSearch(browser, ["duckduckgo"]).search("x")
    assert "не робот" in str(e.value)


def test_youtube_consent_page_is_reported():
    class Consent(FakeBrowser):
        def open(self, url, new_tab=False):
            return FakeInfo("https://consent.youtube.com/m?continue=...")
    with pytest.raises(SearchError) as e:
        WebSearch(Consent({})).youtube("x")
    assert "согласия" in str(e.value)
