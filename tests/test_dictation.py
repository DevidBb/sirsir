import pytest

from conftest import FakeLLM
from jarvis.assistant import Assistant
from jarvis.dictation import Dictation, is_blocked_window, prepare_text
from jarvis.memory import Memory
from jarvis.tool_registry import ToolRegistry


@pytest.mark.parametrize("raw,out", [
    ("привет запятая как дела вопросительный знак", "привет, как дела? "),
    ("Конец предложения точка", "Конец предложения. "),
    ("строка\nс переводом\tи\x1b[31mescape", "строка с переводом и[31mescape "),
    ("открыть скобку пример закрыть скобку", "(пример) "),
    ("   ", ""),
])
def test_prepare_text(raw, out):
    assert prepare_text(raw) == out


def test_prepare_text_never_contains_control_chars():
    text = prepare_text("rm -rf /\r\nshutdown /s\x00\x7f")
    assert all(ch >= " " and ch != "\x7f" for ch in text)


@pytest.mark.parametrize("cls,title,blocked", [
    ("ConsoleWindowClass", "C:\\Windows\\system32\\cmd.exe", True),
    ("CASCADIA_HOSTING_WINDOW_CLASS", "Windows PowerShell", True),
    ("Notepad", "Безымянный — Блокнот", False),
    ("Chrome_WidgetWin_1", "Документ — Google Chrome", False),
    ("Chrome_WidgetWin_1", "Терминал — VS Code", True),
])
def test_blocked_windows(cls, title, blocked):
    assert is_blocked_window(cls, title) == blocked


def make(config, window=("Notepad", "Блокнот")):
    typed = []
    d = Dictation(typer=typed.append, foreground=lambda: window)
    llm = FakeLLM([])
    a = Assistant(config, llm, ToolRegistry(), Memory(config.path("memory.db_path")), dictation=d)
    return a, typed, llm


def test_type_once(config):
    a, typed, llm = make(config)
    reply = a.handle("Напечатай привет мир точка")
    assert reply.text == "" and not reply.expect_followup
    assert typed == ["привет мир. "]
    assert llm.requests == []


def test_dictation_mode(config):
    a, typed, llm = make(config)
    assert "Диктуйте" in a.handle("начни диктовку").text
    assert a.handle("первая строка").text == ""
    assert a.handle("первая строка").text == ""   # повтор в диктовке разрешён
    assert a.handle("открой блокнот").text == ""  # команды не выполняются, а печатаются
    assert a.handle("закончи диктовку").text == "Диктовка завершена."
    assert typed == ["первая строка ", "первая строка ", "открой блокнот "]
    assert not a.dictation.active
    assert llm.requests == []


def test_emergency_stop_works_in_dictation(config):
    a, typed, _ = make(config)
    a.handle("режим диктовки")
    assert a.handle("стоп стоп").action == "stop"
    assert not a.dictation.active


def test_terminal_refused(config):
    a, typed, _ = make(config, window=("ConsoleWindowClass", "cmd.exe"))
    a.handle("начни диктовку")
    reply = a.handle("del /q *")
    assert "терминала" in reply.text and not a.dictation.active
    assert typed == []


def test_not_windows_without_typer(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    assert "только в Windows" in Dictation().type_text("привет")


@pytest.mark.parametrize("phrase", ["Стоп.", "хватит", "всё"])
def test_short_stop_word_ends_dictation(config, phrase):
    a, typed, _ = make(config)
    a.handle("начни диктовку")
    assert a.handle(phrase).text == "Диктовка завершена."
    assert typed == [] and not a.dictation.active
