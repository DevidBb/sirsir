import pytest

from conftest import FakeLLM
from jarvis.assistant import Assistant
from jarvis.calculator import CalcError, calculate, evaluate, format_number, phrase_to_expression
from jarvis.memory import Memory
from jarvis.notes import Notes
from jarvis.tool_registry import ToolRegistry
from jarvis.windows_control import WindowsController


# --- калькулятор ---
@pytest.mark.parametrize("expr,value", [
    ("2+2*2", 6), ("(2+2)*2", 8), ("10/4", 2.5), ("2**10", 1024), ("-3+5", 2),
    ("sqrt(144)", 12), ("7 % 3", 1), ("2,5*2", 5), ("3^2", 9),
])
def test_evaluate(expr, value):
    assert evaluate(expr) == pytest.approx(value)


@pytest.mark.parametrize("expr", [
    "__import__('os').system('dir')", "open('x')", "a+1", "(lambda: 1)()", "[1,2]",
    "1/0", "9**99999", "10**1000**10", "True+1", "x" * 300, "",
])
def test_evaluate_rejects(expr):
    with pytest.raises(CalcError):
        evaluate(expr)


@pytest.mark.parametrize("phrase,expr", [
    ("Сколько будет 15 процентов от 2400?", "2400*15/100"),
    ("сколько будет 15% от 2400", "2400*15/100"),
    ("сколько будет двадцать пять умножить на четыре", "25*4"),
    ("посчитай 100 разделить на 8", "100/8"),
    ("корень из 81", "sqrt(81)"),
    ("сколько будет 7 в квадрате", "7**2"),
    ("сколько будет жить кот", None),
])
def test_phrase_to_expression(phrase, expr):
    assert phrase_to_expression(phrase) == expr


def test_calculate_formats_for_speech():
    assert calculate("10/4") == "Получается 2,5."
    assert calculate("2400*15/100") == "Получается 360."
    assert format_number(1/3) == "0,333333"
    assert "Не могу посчитать" in calculate("1/0")


# --- заметки ---
@pytest.fixture
def notes(tmp_path):
    clock = [1_760_000_000.0]
    n = Notes(Memory(tmp_path / "m.db"), clock=lambda: clock[0])
    n.clock_ref = clock
    return n


def test_notes_add_read_search(notes):
    assert notes.read() == "Заметок нет."
    notes.add("купить молоко")
    notes.clock_ref[0] += 86400
    notes.add("позвонить Ивану")
    text = notes.read()
    assert text.startswith("Последние заметки. сегодня: позвонить Ивану. вчера: купить молоко")
    assert "молоко" in notes.search("молок")
    assert notes.search("хлеб") == "Ничего не нашёл."
    assert notes.delete_last() == "Последняя заметка удалена."
    assert "Ивану" not in notes.read()
    assert notes.clear() == "Удалено заметок: 1."


def test_notes_tools_danger_flags(notes):
    r = ToolRegistry()
    notes.register_tools(r)
    assert r.get("clear_notes").dangerous and r.get("delete_last_note").dangerous
    assert not r.get("add_note").dangerous


# --- папки и питание ---
@pytest.fixture
def ctl(config, tmp_path):
    docs = tmp_path / "Документы"
    docs.mkdir()
    config.set("windows.folders", {"документы": str(docs), "нет такой": str(tmp_path / "nope")})
    launched, power = [], []
    c = WindowsController(config, launcher=launched.append, power=power.append)
    c.launched, c.power_calls, c.docs = launched, power, docs
    return c


def test_open_folder(ctl):
    assert ctl.open_folder("Документы") == "Открываю папку документы."
    assert ctl.launched == [str(ctl.docs)]
    assert "не найдена" in ctl.open_folder("нет такой")
    assert "не в списке" in ctl.open_folder("system32")


def test_power_actions(ctl):
    assert ctl.power_action("lock") == "Блокирую компьютер."
    assert "через 60 секунд" in ctl.power_action("shutdown")
    assert ctl.power_calls == ["lock", "shutdown"]


def test_cancel_without_scheduled_shutdown(config):
    def fail(action):
        raise OSError("nothing")
    c = WindowsController(config, power=fail)
    assert c.power_action("cancel") == "Запланированного выключения нет."


def test_shutdown_by_voice_requires_confirmation(config):
    power = []
    registry = ToolRegistry()
    WindowsController(config, power=power.append).register_tools(registry)
    llm = FakeLLM([])
    a = Assistant(config, llm, registry, Memory(config.path("memory.db_path")))
    reply = a.handle("Выключи компьютер")
    assert "подтверждение" in reply.text.lower()
    assert power == []
    a.handle("подтверждаю")
    assert power == ["shutdown"]
    assert a.handle("Заблокируй компьютер").text == "Блокирую компьютер."  # без подтверждения
    assert power == ["shutdown", "lock"]
    assert llm.requests == []


def test_app_registers_everything(config):
    from jarvis.main import App
    app = App(config, voice=False)
    for name in ("calculate", "add_note", "read_notes", "open_folder", "lock_screen",
                 "shutdown_computer", "set_timer", "media_control"):
        assert name in app.registry.names()
    assert app.assistant.handle("сколько будет 2 плюс 2").text == "Получается 4."
    assert app.assistant.handle("запиши заметку купить хлеб").text == "Записал."
    assert "купить хлеб" in app.assistant.handle("прочитай заметки").text
    app.close()
