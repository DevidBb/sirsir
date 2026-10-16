"""«Открой ютуб»: сбой браузера Джарвиса не должен оставлять пользователя без сайта."""

import webbrowser

from jarvis.browser import BrowserController, BrowserError


def make(tmp_path, error):
    b = BrowserController(tmp_path / "profile")
    def fail(url, new_tab=False):
        raise BrowserError(error)
    b.open = fail
    return b


def test_falls_back_to_default_browser(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    b = make(tmp_path, "Браузер: браузер для Playwright не скачан")
    assert b.open_from_other_thread("https://www.youtube.com") is True
    assert opened == ["https://www.youtube.com"]


def test_slow_page_counts_as_opened(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    b = make(tmp_path, "Браузер: страница не ответила вовремя")
    assert b.open_from_other_thread("https://www.youtube.com") is True
    assert opened == []  # второй вкладки в другом браузере не будет
