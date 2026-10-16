"""Управление браузером через Playwright (отдельный постоянный профиль Джарвиса).

Все вызовы Playwright выполняются в одном служебном потоке: sync API Playwright
привязан к потоку, в котором запущен. Остальные части программы (голосовой цикл,
веб-интерфейс) ставят задачи в очередь через ``BrowserController.call``.

Элементы страницы адресуются номерами из последнего ``read_page`` (атрибут
``data-jarvis-ref``), а не координатами экрана. Номера получаются из DOM и
доступных имён (aria-label, текст, placeholder), поэтому клик «по кнопке 12»
устойчив к изменению размера окна.

Содержимое страниц — недоверенные данные. Они возвращаются модели в обёртке
``untrusted`` и никогда не меняют правила безопасности.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

log = logging.getLogger(__name__)

ALLOWED_SCHEMES = ("http", "https")
# Клик по элементам с такими словами — значимое действие: нужно подтверждение.
RISKY_WORDS = re.compile(
    r"купить|оплат|заказ|оформить|отправить|опубликовать|удалить|подписаться|перевести|"
    r"подтвердить оплату|\bbuy\b|\bpay\b|checkout|purchase|\border\b|\bsend\b|publish|\bpost\b|"
    r"delete|remove|subscribe|transfer|place order|sign up|зарегистрироваться",
    re.I)
MAX_TEXT = 6000
MAX_ELEMENTS = 80

# Собирает видимые интерактивные элементы и помечает их номерами.
COLLECT_JS = r"""
(maxElements) => {
  document.querySelectorAll('[data-jarvis-ref]').forEach(e => e.removeAttribute('data-jarvis-ref'));
  const sel = 'a[href], button, input:not([type=hidden]), textarea, select, summary, ' +
    '[role=button], [role=link], [role=tab], [role=checkbox], [role=menuitem], [role=option], ' +
    '[role=searchbox], [role=textbox], [role=combobox], [role=switch], [contenteditable=true]';
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none' && st.opacity !== '0';
  };
  const inViewport = (el) => {
    const r = el.getBoundingClientRect();
    return r.bottom > 0 && r.top < (window.innerHeight || 800) * 2;
  };
  const label = (el) => {
    const by = el.getAttribute('aria-labelledby');
    let name = el.getAttribute('aria-label') ||
      (by && by.split(' ').map(id => (document.getElementById(id) || {}).innerText || '').join(' ')) ||
      el.getAttribute('alt') || el.getAttribute('title') || el.getAttribute('placeholder') || '';
    if (!name && el.labels && el.labels.length) name = el.labels[0].innerText;
    if (!name) name = el.innerText || '';
    if (!name && el.type !== 'password' && ['submit', 'button'].includes(el.type)) name = el.value || '';
    return name.replace(/\s+/g, ' ').trim().slice(0, 80);
  };
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    if (out.length >= maxElements) break;
    if (!visible(el) || !inViewport(el)) continue;
    n += 1;
    el.setAttribute('data-jarvis-ref', String(n));
    const tag = el.tagName.toLowerCase();
    out.push({
      ref: n, tag,
      role: el.getAttribute('role') || '',
      type: (el.getAttribute('type') || '').toLowerCase(),
      name: label(el),
      href: tag === 'a' ? el.href : '',
      editable: ['input', 'textarea'].includes(tag) || el.isContentEditable || tag === 'select' ||
        ['textbox', 'searchbox', 'combobox'].includes(el.getAttribute('role') || ''),
    });
  }
  return out;
}
"""

TEXT_JS = r"""
() => {
  const root = document.querySelector('main, article, [role=main]') || document.body;
  return (root ? root.innerText : '').replace(/\n{3,}/g, '\n\n');
}
"""


class BrowserError(RuntimeError):
    """Понятная пользователю ошибка браузера."""


@dataclass
class Element:
    ref: int
    tag: str
    role: str
    type: str
    name: str
    href: str
    editable: bool

    def describe(self) -> str:
        kind = self.role or ("ссылка" if self.tag == "a" else
                             "поле" if self.editable else self.tag)
        if self.type and self.tag == "input":
            kind = f"поле:{self.type}"
        text = f"[{self.ref}] {kind} «{self.name or '—'}»"
        if self.href:
            text += f" → {self.href[:120]}"
        return text


@dataclass
class PageInfo:
    title: str
    url: str
    text: str
    elements: list[Element] = field(default_factory=list)


def check_url(url: str) -> str:
    """Только http(s): file://, javascript: и chrome:// из модели не открываются."""
    url = (url or "").strip()
    if not url:
        raise BrowserError("Не указан адрес.")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        # «javascript:…», «data:…» — схема без //; «site.ru:8080» — это порт, не схема.
        scheme = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):(?!\d)", url)
        if scheme:
            raise BrowserError(f"Адрес с такой схемой открывать нельзя: {scheme.group(1)}")
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES or not parsed.netloc:
        raise BrowserError(f"Адрес с такой схемой открывать нельзя: {parsed.scheme or url}")
    return url


def is_risky_name(name: str) -> bool:
    return bool(RISKY_WORDS.search(name or ""))


class BrowserController:
    """Видимый Chromium с профилем ``data/browser-profile``.

    ``launcher`` подменяется в тестах; по умолчанию запускается Playwright.
    """

    def __init__(self, profile_dir: str | Path, headless: bool = False, channel: str = "chromium",
                 timeout_ms: int = 20000, browsers_path: str | Path | None = None,
                 screenshots_dir: str | Path | None = None,
                 launcher: Callable[["BrowserController"], Any] | None = None):
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.channel = channel
        self.timeout_ms = timeout_ms
        self.browsers_path = Path(browsers_path) if browsers_path else None
        self.screenshots_dir = Path(screenshots_dir or self.profile_dir.parent / "screenshots")
        self._launcher = launcher
        self._jobs: queue.Queue[tuple[Callable[[], Any], Future] | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._pw = None
        self.context = None
        self.page = None
        self.last_elements: dict[int, Element] = {}
        self.last_error = ""

    # --- служебный поток ---
    def _worker(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                break
            fn, fut = job
            if fut.set_running_or_notify_cancel():
                try:
                    fut.set_result(fn())
                except BaseException as e:  # noqa: BLE001 — передаём вызывающему
                    fut.set_exception(e)
        self._shutdown()

    def call(self, fn: Callable[[], Any], timeout: float | None = None) -> Any:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._worker, name="browser", daemon=True)
                self._thread.start()
        fut: Future = Future()
        self._jobs.put((fn, fut))
        return fut.result(timeout=timeout if timeout is not None else self.timeout_ms / 1000 * 3)

    @property
    def running(self) -> bool:
        return self.context is not None

    def _ensure(self) -> None:
        if self.context is not None:
            try:
                _ = self.context.pages  # браузер закрыли вручную?
                if self.page is None or self.page.is_closed():
                    pages = self.context.pages
                    self.page = pages[-1] if pages else self.context.new_page()
                return
            except Exception:  # noqa: BLE001
                self.context = None
        if self._launcher is not None:
            self.context = self._launcher(self)
        else:
            self.context = self._launch_playwright()
        pages = self.context.pages
        self.page = pages[0] if pages else self.context.new_page()
        self.context.on("page", self._on_new_page)

    def _launch_playwright(self):
        if self.browsers_path and self.browsers_path.exists():
            os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(self.browsers_path))
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise BrowserError("Не установлен Playwright: pip install -r requirements.txt") from e
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        kwargs: dict[str, Any] = {"headless": self.headless, "viewport": None,
                                  "args": ["--start-maximized"]}
        if self.channel:
            kwargs["channel"] = self.channel
        try:
            context = self._pw.chromium.launch_persistent_context(str(self.profile_dir), **kwargs)
        except Exception as e:  # noqa: BLE001
            self._pw.stop()
            self._pw = None
            msg = str(e).splitlines()[0]
            if "Executable doesn't exist" in str(e):
                msg = ("браузер для Playwright не скачан. Выполните: "
                       "python -m playwright install chromium")
            raise BrowserError(f"Не удалось запустить браузер: {msg}") from e
        context.set_default_timeout(self.timeout_ms)
        context.set_default_navigation_timeout(self.timeout_ms * 2)
        log.info("Браузер запущен, профиль %s", self.profile_dir)
        return context

    def _on_new_page(self, page) -> None:
        # Ссылка открылась в новой вкладке — работаем с ней.
        self.page = page

    def _shutdown(self) -> None:
        try:
            if self.context is not None:
                self.context.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self.context = self.page = self._pw = None

    def close(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._jobs.put(None)
            self._thread.join(timeout=10)

    # --- действия (выполняются в служебном потоке) ---
    def _run(self, fn: Callable[[], Any]) -> Any:
        def job():
            self._ensure()
            return fn()
        try:
            result = self.call(job)
            self.last_error = ""
            return result
        except BrowserError as e:
            self.last_error = str(e)
            raise
        except Exception as e:  # noqa: BLE001 — переводим ошибки Playwright в понятные
            text = str(e).splitlines()[0] if str(e) else type(e).__name__
            if "Timeout" in type(e).__name__ or "Timeout" in text:
                text = "страница не ответила вовремя"
            self.last_error = text
            raise BrowserError(f"Браузер: {text}") from e

    def _wait_settled(self) -> None:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)
            self.page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:  # noqa: BLE001 — сеть может не затихнуть (видео, реклама)
            pass

    def open(self, url: str, new_tab: bool = False) -> PageInfo:
        url = check_url(url)

        def go():
            if new_tab:
                self.page = self.context.new_page()
            self.page.goto(url, wait_until="domcontentloaded")
            self.page.bring_to_front()
            self._wait_settled()
            return self._info(with_elements=False)
        return self._run(go)

    def open_from_other_thread(self, url: str) -> bool:
        """Совместимо с webbrowser.open для быстрых команд «открой ютуб»."""
        try:
            self.open(url)
            return True
        except BrowserError as e:
            log.warning("%s", e)
            return False

    def _info(self, with_elements: bool = True, max_text: int = MAX_TEXT) -> PageInfo:
        page = self.page
        text = ""
        try:
            text = page.evaluate(TEXT_JS) or ""
        except Exception:  # noqa: BLE001 — страница перезагружается
            pass
        elements: list[Element] = []
        if with_elements:
            raw = page.evaluate(COLLECT_JS, MAX_ELEMENTS) or []
            elements = [Element(**{k: item.get(k, "") for k in Element.__dataclass_fields__})
                        for item in raw]
            self.last_elements = {e.ref: e for e in elements}
        return PageInfo(page.title(), page.url, text[:max_text], elements)

    def read(self, max_text: int = MAX_TEXT) -> PageInfo:
        return self._run(lambda: self._info(True, max_text))

    def evaluate(self, script: str, arg: Any = None) -> Any:
        """Только для встроенных модулей (поиск); модели этот метод не доступен."""
        return self._run(lambda: self.page.evaluate(script, arg))

    def element(self, ref: int) -> Element:
        el = self.last_elements.get(int(ref))
        if el is None:
            raise BrowserError(f"Нет элемента [{ref}]. Сначала прочитайте страницу заново.")
        return el

    def _locator(self, ref: int):
        loc = self.page.locator(f'[data-jarvis-ref="{int(ref)}"]')
        if loc.count() == 0:
            raise BrowserError(f"Элемент [{ref}] исчез со страницы. Прочитайте страницу заново.")
        return loc.first

    def click(self, ref: int) -> PageInfo:
        def do():
            before = self.page.url
            self._locator(ref).click()
            self._wait_settled()
            info = self._info(with_elements=False)
            if info.url != before:
                log.info("Переход: %s -> %s", before, info.url)
            return info
        return self._run(do)

    def type(self, ref: int, text: str, submit: bool = False) -> PageInfo:
        def do():
            loc = self._locator(ref)
            loc.fill(text)
            if submit:
                loc.press("Enter")
                self._wait_settled()
            return self._info(with_elements=False)
        return self._run(do)

    def press(self, key: str) -> PageInfo:
        def do():
            self.page.keyboard.press(key)
            self._wait_settled()
            return self._info(with_elements=False)
        return self._run(do)

    def scroll(self, direction: str = "down") -> PageInfo:
        def do():
            delta = {"down": 700, "up": -700}.get(direction, 700)
            if direction == "top":
                self.page.evaluate("() => window.scrollTo(0, 0)")
            elif direction == "bottom":
                self.page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            else:
                self.page.mouse.wheel(0, delta)
            self.page.wait_for_timeout(400)
            return self._info(with_elements=False)
        return self._run(do)

    def back(self) -> PageInfo:
        def do():
            self.page.go_back()
            self._wait_settled()
            return self._info(with_elements=False)
        return self._run(do)

    def tabs(self) -> list[dict[str, Any]]:
        def do():
            out = []
            for i, p in enumerate(self.context.pages):
                try:
                    title = p.title()
                except Exception:  # noqa: BLE001
                    title = ""
                out.append({"index": i + 1, "title": title, "url": p.url, "active": p is self.page})
            return out
        return self._run(do)

    def switch_tab(self, index: int) -> PageInfo:
        def do():
            pages = self.context.pages
            if not 1 <= index <= len(pages):
                raise BrowserError(f"Нет вкладки {index}, открыто {len(pages)}.")
            self.page = pages[index - 1]
            self.page.bring_to_front()
            return self._info(with_elements=False)
        return self._run(do)

    def close_tab(self, index: int | None = None) -> str:
        def do():
            pages = self.context.pages
            target = self.page if index is None else (pages[index - 1] if 1 <= index <= len(pages)
                                                      else None)
            if target is None:
                raise BrowserError(f"Нет вкладки {index}.")
            target.close()
            pages = self.context.pages
            self.page = pages[-1] if pages else self.context.new_page()
            return f"Вкладка закрыта, осталось {len(pages)}."
        return self._run(do)

    def screenshot(self) -> Path:
        def do():
            self.screenshots_dir.mkdir(parents=True, exist_ok=True)
            path = self.screenshots_dir / time.strftime("shot-%Y%m%d-%H%M%S.png")
            self.page.screenshot(path=str(path))
            return path
        return self._run(do)

    def status(self) -> dict[str, Any]:
        info: dict[str, Any] = {"running": self.running, "profile": str(self.profile_dir),
                                "headless": self.headless, "last_error": self.last_error}
        if self.running and self.page is not None:
            try:
                info["url"] = self.call(lambda: self.page.url, timeout=3)
            except Exception:  # noqa: BLE001
                pass
        return info
