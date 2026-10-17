"""«Открой что угодно»: меню «Пуск», сайты, папки и файлы пользователя (без Windows API)."""

from pathlib import Path

import pytest

from conftest import FakeLLM
from jarvis.app_finder import (StartMenuIndex, parse_lnk, parse_url_file, site_url,
                               unsafe_target_reason)
from jarvis.assistant import Assistant
from jarvis.fuzzy import similarity
from jarvis.intents import Intent, IntentMatcher
from jarvis.memory import Memory
from jarvis.tool_registry import Choice, ToolRegistry
from jarvis.windows_control import WindowsController


def make_lnk(target: str, args: str = "") -> bytes:
    """Минимальный .lnk (MS-SHLLINK): LinkInfo с LocalBasePath и, при желании, аргументы."""
    flags = 0x02 | 0x80 | (0x20 if args else 0)
    header = (0x4C).to_bytes(4, "little") + b"\0" * 16 + flags.to_bytes(4, "little")
    header += b"\0" * (0x4C - len(header))
    volume = (0x10).to_bytes(4, "little") + b"\0" * 12
    base = target.encode("cp1251") + b"\0"
    li_header = 0x1C
    body = volume + base + b"\0"
    li = (li_header + len(body)).to_bytes(4, "little") + li_header.to_bytes(4, "little")
    li += (1).to_bytes(4, "little") + li_header.to_bytes(4, "little")
    li += (li_header + len(volume)).to_bytes(4, "little") + b"\0" * 4
    li += (li_header + len(volume) + len(base)).to_bytes(4, "little")
    data = header + li + body
    if args:
        data += len(args).to_bytes(2, "little") + args.encode("utf-16-le")
    return data + b"\0" * 4


@pytest.mark.parametrize("query,name,ok", [
    ("телеграм", "Telegram", True),
    ("стим", "Steam", True),
    ("фотошоп", "Adobe Photoshop 2024", True),
    ("дискорд", "Discord", True),
    ("хром", "Google Chrome", True),
    ("блокнот", "Блокнот", True),
    ("телеграм", "Paint", False),
    ("отчет", "Outlook", False),
])
def test_similarity(query, name, ok):
    assert (similarity(query, name) >= 0.75) == ok


def test_parse_lnk_and_safety():
    target, args = parse_lnk(make_lnk(r"C:\Program Files\Telegram\Telegram.exe"))
    assert target == r"C:\Program Files\Telegram\Telegram.exe" and args == ""
    target, args = parse_lnk(make_lnk(r"C:\Windows\System32\cmd.exe", "/c evil.bat"))
    assert target.endswith("cmd.exe") and args == "/c evil.bat"
    assert parse_lnk(b"not a link") == (None, "")
    assert unsafe_target_reason(r"C:\Program Files\Steam\steam.exe") is None
    assert unsafe_target_reason(None) is None
    assert unsafe_target_reason(r"C:\Users\me\Downloads\setup.exe")
    assert unsafe_target_reason(r"C:\Users\me\AppData\Local\Temp\x.exe")
    assert unsafe_target_reason(r"C:\Tools\run.bat")
    assert unsafe_target_reason(r"C:\Tools\a.ps1")
    assert unsafe_target_reason(r"C:\Tools\a.vbs")
    assert unsafe_target_reason(r"C:\Windows\system32\compmgmt.msc") is None
    assert unsafe_target_reason(r"C:\Users\me\Downloads\evil.msc")
    assert unsafe_target_reason(r"C:\Windows\System32\cmd.exe", "/c del *")
    assert unsafe_target_reason(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe") is None
    assert parse_url_file("[InternetShortcut]\nURL=steam://rungameid/570\n") == "steam://rungameid/570"


@pytest.fixture
def start_menu(tmp_path):
    common = tmp_path / "ProgramData" / "Programs"
    user = tmp_path / "AppData" / "Programs"
    (common / "Adobe").mkdir(parents=True)
    user.mkdir(parents=True)
    (user / "Telegram.lnk").write_bytes(make_lnk(r"C:\Users\me\AppData\Roaming\Telegram Desktop\Telegram.exe"))
    (common / "Telegram.lnk").write_bytes(make_lnk(r"C:\Program Files\Telegram\Telegram.exe"))
    (common / "Steam.lnk").write_bytes(make_lnk(r"C:\Program Files (x86)\Steam\steam.exe"))
    (common / "Uninstall Steam.lnk").write_bytes(make_lnk(r"C:\Program Files (x86)\Steam\uninstall.exe"))
    (common / "Adobe" / "Adobe Photoshop 2024.lnk").write_bytes(make_lnk(r"C:\Adobe\Photoshop.exe"))
    (common / "Photoshop Express.lnk").write_bytes(make_lnk(r"C:\Adobe\PSX.exe"))
    (common / "Dota 2.url").write_text("[InternetShortcut]\nURL=steam://rungameid/570\n")
    (common / "Злой ярлык.url").write_text("[InternetShortcut]\nURL=file:///C:/Windows/System32/cmd.exe\n")
    (common / "Загрузчик.lnk").write_bytes(make_lnk(r"C:\Users\me\Downloads\loader.exe"))
    (common / "Скрипт.lnk").write_bytes(make_lnk(r"C:\Tools\install.cmd"))
    (common / "readme.txt").write_text("не ярлык")
    return [common, user]


@pytest.fixture
def ctl(config, start_menu):
    launched, opened = [], []
    index = StartMenuIndex(start_menu)
    c = WindowsController(config, launcher=launched.append, url_opener=opened.append,
                          start_menu=index)
    c.launched, c.opened, c.index = launched, opened, index
    return c


def test_start_menu_index_cache_and_filters(start_menu):
    t = [0.0]
    index = StartMenuIndex(start_menu, cache_seconds=600, clock=lambda: t[0])
    names = sorted(s.name for s in index.shortcuts())
    assert "Uninstall Steam" not in names and "readme" not in names
    assert names.count("Telegram") == 1          # одинаковый ярлык «для всех» и «для себя»
    index.shortcuts()
    assert index.scans == 1                      # второй раз — из кэша
    t[0] = 601
    index.shortcuts()
    assert index.scans == 2                      # кэш устарел — пересканировали
    index.refresh()
    index.shortcuts()
    assert index.scans == 3


def test_open_app_from_start_menu(ctl):
    assert ctl.open_app("телеграм") == "Запускаю Telegram."
    assert Path(ctl.launched[-1]).name == "Telegram.lnk"   # запускается сам ярлык
    assert ctl.open_app("стим") == "Запускаю Steam."
    assert Path(ctl.launched[-1]).name == "Steam.lnk"
    assert ctl.open_app("блокнот") == "Запускаю блокнот."  # белый список по-прежнему первым
    assert ctl.launched[-1] == "notepad.exe"


def test_ambiguous_app_asks(ctl):
    result = ctl.open_app("фотошоп")
    assert isinstance(result, Choice) and "Какой открыть" in result
    assert sorted(label for label, _ in result.options) == ["Adobe Photoshop 2024", "Photoshop Express"]
    assert ctl.launched == []


def test_unsafe_shortcuts_refused(ctl):
    assert "загрузок" in ctl.open_app("загрузчик")
    assert "скрипт" in ctl.open_app("скрипт")
    assert "неразрешённый адрес" in ctl.open_app("злой ярлык")
    assert ctl.launched == []
    assert ctl.open_app("дота 2") == "Запускаю Dota 2."   # steam:// разрешён


@pytest.mark.parametrize("spoken,url", [
    ("habr.com", "https://habr.com"),
    ("Habr.com.", "https://habr.com"),
    ("хабр точка ком", "https://habr.com"),       # кириллица в домене .com → латиница
    ("habr точка ком", "https://habr.com"),
    ("vk.com/feed", "https://vk.com/feed"),
    ("http://example.org", "http://example.org"),
    ("кц.рф", "https://кц.рф"),
    ("javascript:alert(1)", None),
    ("file:///C:/Windows", None),
    ("ftp://example.com", None),
    ("user@evil.com", None),
    ("отчёт.docx", None),
    ("телеграм", None),
])
def test_site_url(spoken, url):
    assert site_url(spoken) == url


def test_open_websites_by_name_and_address(ctl):
    assert ctl.open_anything("вконтакте") == "Открываю вконтакте."
    assert ctl.opened == ["https://vk.com"]
    assert ctl.open_anything("habr.com", "site") == "Открываю habr.com."
    assert ctl.opened[-1] == "https://habr.com"
    assert ctl.launched == []


@pytest.mark.parametrize("spoken", ["YouTube", "you tube", "ютюб", "ютубе"])
def test_open_website_by_spoken_variant(ctl, spoken):
    # Whisper пишет «YouTube» латиницей; нечёткое сравнение с «ютуб» его не ловило.
    assert ctl.open_anything(spoken) == "Открываю ютуб."
    assert ctl.opened == ["https://www.youtube.com"]


def test_open_user_folders_and_files(ctl, config, tmp_path):
    base = Path(config.get("windows.search.allowed_dirs")[0])
    (base / "Отчёты").mkdir()
    (base / "Отчёты" / "Годовой_отчёт.docx").write_text("x")
    (base / "Отпуск.txt").write_text("x")
    (base / "отпуск.exe").write_text("MZ")
    (base / "отпуск.bat").write_text("del *")
    (tmp_path / "Годовой_отчёт_снаружи.docx").write_text("x")
    assert ctl.open_folder("отчёты") == "Открываю папку Отчёты."
    assert ctl.launched[-1] == str(base / "Отчёты")
    assert ctl.open_file("годовой отчёт") == "Открываю файл Годовой_отчёт.docx."
    assert ctl.open_anything("отпуск") == "Открываю файл Отпуск.txt."  # .exe и .bat не предлагаются
    assert not any(p.endswith((".exe", ".bat")) for p in ctl.launched)
    assert "не найден" in ctl.open_file("снаружи")


def test_open_anything_nothing_found(ctl):
    assert ctl.open_anything("абракадабра") .startswith("Не нашёл")
    assert ctl.launched == [] and ctl.opened == []


@pytest.mark.parametrize("phrase,intent", [
    ("открой телеграм", Intent("open_anything", {"name": "телеграм"})),
    ("Запусти, пожалуйста, стим.", Intent("open_anything", {"name": "стим"})),
    ("открой фотошоп", Intent("open_anything", {"name": "фотошоп"})),
    ("открой вконтакте", Intent("open_anything", {"name": "вконтакте"})),
    ("Зайди на habr.com", Intent("open_anything", {"name": "habr.com", "kind": "site"})),
    ("открой сайт хабр", Intent("open_anything", {"name": "хабр", "kind": "site"})),
    ("открой папку отчёты", Intent("open_anything", {"name": "отчеты", "kind": "folder"})),
    ("открой файл отчёт.docx", Intent("open_anything", {"name": "отчет docx", "kind": "file"})),
    ("открой отчёт.docx", Intent("open_anything", {"name": "отчет docx"})),
    ("открой блокнот", Intent("open_app", {"name": "блокнот"})),   # белый список — как раньше
])
def test_open_intents(phrase, intent):
    m = IntentMatcher({"блокнот": "notepad.exe"}, {"ютуб": "https://www.youtube.com"})
    assert m.match(phrase) == intent


@pytest.mark.parametrize("phrase", ["включи музыку", "открой окно"])
def test_open_intents_not_hijacked(phrase):
    assert IntentMatcher({}, {}).match(phrase) is None


def build(config, ctl):
    registry = ToolRegistry()
    ctl.register_tools(registry)
    llm = FakeLLM([])
    a = Assistant(config, llm, registry, Memory(config.path("memory.db_path")))
    a.fake = llm
    return a


def test_assistant_asks_and_picks_by_number(config, ctl):
    a = build(config, ctl)
    reply = a.handle("открой фотошоп")
    assert "Нашёл несколько вариантов" in reply.text and reply.expect_followup
    first_label = reply.text.split("1) ")[1].split(";")[0]
    reply = a.handle("второй")
    assert reply.text.startswith("Запускаю") and first_label not in reply.text
    assert len(ctl.launched) == 1 and a.fake.requests == []


def test_assistant_picks_by_name(config, ctl):
    a = build(config, ctl)
    a.handle("открой фотошоп")
    assert a.handle("экспресс").text == "Запускаю Photoshop Express."
    assert Path(ctl.launched[-1]).name == "Photoshop Express.lnk"


def test_assistant_choice_cancel_and_unrelated(config, ctl):
    a = build(config, ctl)
    a.handle("открой фотошоп")
    assert a.handle("отмена").text == "Хорошо, не открываю."
    assert ctl.launched == []
    a.handle("открой фотошоп")
    a.handle("который час")       # не выбор — обычная команда, вопрос снимается
    a.handle("второй")
    assert ctl.launched == []


def test_assistant_choice_expires(config, ctl):
    a = build(config, ctl)
    t = [0.0]
    a.clock = lambda: t[0]
    a.handle("открой фотошоп")
    t[0] = 1000
    a.handle("первый")
    assert ctl.launched == []


def test_assistant_opens_site_by_address(config, ctl):
    a = build(config, ctl)
    assert a.handle("Зайди на habr.com").text == "Открываю habr.com."
    assert ctl.opened == ["https://habr.com"]
