"""Восстановление браузера после ручного закрытия окна — без Chromium (поддельный Playwright)."""

import pytest

from jarvis.browser import BrowserController, BrowserError, is_closed_error

CLOSED = "Page.goto: Target page, context or browser has been closed"


class FakePage:
    def __init__(self, ctx):
        self.ctx, self.url, self.closed = ctx, "about:blank", False

    def is_closed(self):
        return self.closed

    def goto(self, url, wait_until=None):
        if self.ctx.dead:
            raise RuntimeError(CLOSED)
        self.url = url

    def bring_to_front(self):
        pass

    def wait_for_load_state(self, *a, **k):
        pass

    def evaluate(self, *a):
        return ""

    def title(self):
        return "t"


class FakeContext:
    def __init__(self):
        self.dead, self.closed = False, 0
        self.pages = [FakePage(self)]

    def on(self, *a):
        pass

    def new_page(self):
        if self.dead:
            raise RuntimeError(CLOSED)
        self.pages.append(FakePage(self))
        return self.pages[-1]

    def close(self):
        self.closed += 1


@pytest.fixture
def browser(tmp_path):
    contexts = []

    def launcher(_b):
        contexts.append(FakeContext())
        return contexts[-1]
    b = BrowserController(tmp_path / "profile", launcher=launcher)
    b.contexts = contexts
    yield b
    b.close()


def test_closed_window_is_relaunched_and_action_repeated_once(browser):
    browser.open("https://example.com/a")
    browser.contexts[0].dead = True  # пользователь закрыл окно; Playwright ещё не знает об этом
    info = browser.open("https://example.com/b")
    assert info.url == "https://example.com/b"
    assert len(browser.contexts) == 2 and browser.contexts[0].closed == 1  # старый освобождён


def test_closed_twice_in_a_row_is_reported_not_looped(browser):
    def always_dead(_b):
        ctx = FakeContext()
        ctx.dead = True
        browser.contexts.append(ctx)
        return ctx
    browser._launcher = always_dead
    with pytest.raises(BrowserError, match="has been closed"):
        browser.open("https://example.com/")
    assert len(browser.contexts) == 2  # один перезапуск, без бесконечных повторов


def test_other_errors_are_not_retried(browser):
    browser.open("https://example.com/")
    with pytest.raises(BrowserError, match="другая ошибка"):
        browser._run(lambda: (_ for _ in ()).throw(RuntimeError("другая ошибка")))
    assert len(browser.contexts) == 1


def test_closed_error_markers():
    assert is_closed_error(RuntimeError(CLOSED))
    assert is_closed_error(RuntimeError("Target closed"))
    assert not is_closed_error(RuntimeError("Timeout 20000ms exceeded"))
