"""Поиск на сайтах и в приложениях, составные команды «открой X и сделай Y»."""

from urllib.parse import parse_qs, urlsplit

import pytest

from conftest import FakeLLM
from jarvis.app_finder import SEARCH_SITES, search_url
from jarvis.assistant import Assistant
from jarvis.dictation import Dictation
from jarvis.intents import Intent, IntentMatcher
from jarvis.memory import Memory
from jarvis.tool_registry import ToolRegistry
from jarvis.windows_control import WindowsController


@pytest.mark.parametrize("phrase,args", [
    ("найди на ютубе котиков", {"query": "котиков", "site": "youtube"}),
    ("Открой ютуб и включи lofi hip hop", {"query": "lofi hip hop", "site": "youtube"}),
    ("привет, открой ютуб и найди котиков", {"query": "котиков", "site": "youtube"}),
    ("загугли погоду в Москве", {"query": "погоду в москве", "site": "google"}),
    ("найди в интернете рецепт борща", {"query": "рецепт борща"}),
    ("включи песню Imagine Dragons Believer", {"query": "imagine dragons believer", "site": "youtube"}),
    ("найди в википедии чёрная дыра", {"query": "черная дыра", "site": "wikipedia"}),
    ("включи в спотифае рок", {"query": "рок", "site": "spotify"}),
    ("найди на озоне наушники", {"query": "наушники", "site": "ozon"}),
    ("найди в яндекс музыке Кино", {"query": "кино", "site": "yandex_music"}),
    ("найди на карте аптеку", {"query": "аптеку", "site": "maps"}),
])
def test_search_intents(phrase, args):
    assert IntentMatcher({}, {}).match(phrase) == Intent("web_search", args)


@pytest.mark.parametrize("phrase", ["поставь музыку на паузу", "включи музыку", "найди файл отчёт",
                                    "включи следующий трек"])
def test_search_does_not_hijack(phrase):
    intent = IntentMatcher({}, {}).match(phrase)
    assert intent is None or intent.tool != "web_search"


def test_greeting_does_not_break_quick_commands():
    m = IntentMatcher({"блокнот": "notepad.exe"}, {})
    assert m.match("Привет, открой блокнот") == Intent("open_app", {"name": "блокнот"})
    assert m.match("привет") is None  # просто приветствие — ответит LLM


def test_search_url_is_encoded():
    url = search_url("youtube", "котики & собаки/../?x=1")
    parts = urlsplit(url)
    assert parts.scheme == "https" and parts.hostname == "www.youtube.com"
    assert parse_qs(parts.query) == {"search_query": ["котики & собаки/../?x=1"]}
    assert search_url("spotify", "rock & roll") == "spotify:search:rock%20%26%20roll"
    assert all(t.startswith(("https://", "spotify:")) for _, t, _ in SEARCH_SITES.values())


@pytest.fixture
def ctl(config):
    launched, opened = [], []
    c = WindowsController(config, launcher=launched.append, url_opener=opened.append)
    c.launched, c.opened = launched, opened
    return c


def test_web_search_tool(ctl):
    assert ctl.web_search("котиков", "youtube") == "Ищу «котиков»: YouTube."
    assert ctl.opened == ["https://www.youtube.com/results?search_query=%D0%BA%D0%BE%D1%82%D0%B8%D0%BA%D0%BE%D0%B2"]
    ctl.web_search("погода")                       # по умолчанию — Google из config.toml
    assert ctl.opened[-1].startswith("https://www.google.com/search?q=")
    assert ctl.web_search("рок", "spotify") == "Ищу «рок» в Spotify."
    assert ctl.launched == ["spotify:search:%D1%80%D0%BE%D0%BA"]


# --- составные команды --------------------------------------------------------
class Desktop:
    """Фейковый рабочий стол: запуск программы делает её окно активным."""

    def __init__(self, opens_window=True):
        self.active = ("ConsoleWindowClass", "JARVIS — cmd")
        self.opens_window = opens_window
        self.launched, self.opened = [], []

    def launch(self, command):
        self.launched.append(command)
        if self.opens_window:
            self.active = ("Notepad", "Безымянный — Блокнот")

    def open_url(self, url):
        self.opened.append(url)
        self.active = ("Chrome_WidgetWin_1", "YouTube — Google Chrome")


def build(config, desktop):
    typed, keys = [], []
    d = Dictation(typer=typed.append, foreground=lambda: desktop.active, key_sender=keys.append,
                  sleep=lambda s: None)
    registry = ToolRegistry()
    WindowsController(config, launcher=desktop.launch, url_opener=desktop.open_url).register_tools(registry)
    llm = FakeLLM([])
    a = Assistant(config, llm, registry, Memory(config.path("memory.db_path")), dictation=d)
    a.typed, a.keys, a.fake = typed, keys, llm
    return a


def test_open_and_type(config):
    desk = Desktop()
    a = build(config, desk)
    reply = a.handle("Открой блокнот и напиши привет, как дела?")
    assert reply.text == "Запускаю блокнот."
    assert desk.launched == ["notepad.exe"]
    assert a.typed == ["привет, как дела? "]
    assert a.fake.requests == []


def test_open_then_hotkey(config):
    desk = Desktop()
    a = build(config, desk)
    a.handle("запусти блокнот, потом вставь")
    assert desk.launched == ["notepad.exe"] and a.keys == [[0x11, ord("V")]]


def test_no_typing_if_window_did_not_appear(config):
    desk = Desktop(opens_window=False)   # фокус остался на прежнем окне
    a = build(config, desk)
    reply = a.handle("открой блокнот и напиши секрет")
    assert "печатать не стал" in reply.text and a.typed == []


def test_open_site_and_search_is_one_search(config):
    desk = Desktop()
    a = build(config, desk)
    a.handle("открой ютуб и найди котиков")
    assert desk.opened == ["https://www.youtube.com/results?search_query=%D0%BA%D0%BE%D1%82%D0%B8%D0%BA%D0%BE%D0%B2"]


def test_first_part_failed_stops(config):
    desk = Desktop()
    a = build(config, desk)
    reply = a.handle("открой абракадабру и напиши привет")
    assert reply.text.startswith("Не нашёл") and a.typed == []



def test_composite_chatgpt_prompt_is_pinned_to_chatgpt_window(config):
    from jarvis.assistant import Reply

    a = build(config, Desktop())
    targets = []
    a.dictation.foreground_title = lambda: "Untitled - Notepad"
    a.dictation.wait_new_foreground = lambda before, timeout=8.0, step=0.25: "ChatGPT — Google Chrome"
    a._quick_intent = lambda phrase: Reply("Открываю сайт ChatGPT.")
    a._input_intent = lambda phrase, intent, target_window=None: (
        targets.append(target_window), Reply("Текст подготовлен."))[1]

    reply = a._composite("Открой ChatGPT и напиши промпт для создания сайта.")
    assert reply is not None and "Текст подготовлен." in reply.text
    assert targets == ["ChatGPT"]
