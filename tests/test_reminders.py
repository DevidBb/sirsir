import datetime as dt
import threading

import pytest

from jarvis.memory import Memory
from jarvis.reminders import (Reminders, next_time_of_day, parse_duration, parse_number,
                              say_duration)
from jarvis.tool_registry import ToolRegistry


class Clock:
    def __init__(self, t=None):
        self.t = t or dt.datetime(2026, 10, 15, 12, 0).timestamp()

    def __call__(self):
        return self.t


@pytest.fixture
def rem(tmp_path):
    clock = Clock()
    r = Reminders(Memory(tmp_path / "m.db"), clock=clock)
    r.clock_obj = clock
    return r


@pytest.mark.parametrize("text,value", [("5", 5), ("пять", 5), ("двадцать пять", 25),
                                        ("одну", 1), ("сто", 100), ("двести пятьдесят", 250),
                                        ("двух тысяч", 2000), ("тысяча двести", 1200),
                                        ("пять двадцать", None), ("одиннадцать два", None),
                                        ("котик", None), ("", None)])
def test_parse_number(text, value):
    assert parse_number(text) == value


@pytest.mark.parametrize("text,seconds", [
    ("на 5 минут", 300), ("пять минут", 300), ("через 2 часа", 7200), ("полчаса", 1800),
    ("на минуту", 60), ("час", 3600), ("30 секунд", 30), ("полтора часа", 5400),
    ("на завтра", None), ("красный", None),
    # составные длительности
    ("1 минуту 30 секунд", 90), ("час и 15 минут", 4500), ("час двадцать минут", 4800),
    ("два часа десять минут", 7800), ("одну минуту тридцать секунд", 90),
    ("5 минут 5 минут", None), ("на варку яиц", None),
    ("на полторы минуты", 90), ("на 1,5 минуты", 90), ("1.5 часа", 5400), ("полминуты", 30),
    ("сто двадцать секунд", 120),
])
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("seconds,text", [(60, "1 минуту"), (120, "2 минуты"), (300, "5 минут"),
                                          (3660, "1 час 1 минуту"), (21, "21 секунду"),
                                          (7200, "2 часа"), (11 * 60, "11 минут")])
def test_say_duration(seconds, text):
    assert say_duration(seconds) == text


def test_next_time_of_day():
    now = dt.datetime(2026, 10, 15, 12, 0)
    assert next_time_of_day("18:30", now) == dt.datetime(2026, 10, 15, 18, 30)
    assert next_time_of_day("9 05", now) == dt.datetime(2026, 10, 16, 9, 5)
    assert next_time_of_day("25:00", now) is None


def test_reminder_fires_once(rem):
    assert rem.add("позвонить маме", 600) == "Напомню через 10 минут: позвонить маме."
    assert rem.pop_due() == []
    rem.clock_obj.t += 601
    due = rem.pop_due()
    assert [d[0] for d in due] == ["позвонить маме"]
    assert rem.pop_due() == []
    assert rem.announcement(*due[0], now=rem.clock_obj.t) == "Напоминание: позвонить маме."


def test_missed_reminder_after_restart(tmp_path):
    clock = Clock()
    r1 = Reminders(Memory(tmp_path / "m.db"), clock=clock)
    r1.add("выпить таблетку", 60)
    r1.memory.close()
    clock.t += 3600  # помощник был выключен час
    r2 = Reminders(Memory(tmp_path / "m.db"), clock=clock)
    text, kind, due = r2.pop_due()[0]
    assert r2.announcement(text, kind, due, clock.t) == "Пропущенное напоминание: выпить таблетку."


def test_limits(rem):
    assert "в будущем" in rem.add("x", 0)
    assert "неделю" in rem.add("x", 8 * 24 * 3600)


def test_add_at(rem):
    assert rem.add_at("забрать посылку", "18:30") == "Напомню сегодня в 18:30: забрать посылку."
    assert rem.add_at("встать", "07:00") == "Напомню завтра в 07:00: встать."
    assert "Не понял" in rem.add_at("x", "утром")


def test_list_and_cancel(rem):
    rem.add("таймер", 300, "timer")
    rem.add("позвонить маме", 600)
    listing = rem.describe_pending()
    assert "Активно 2" in listing and "таймер, ещё 5 минут" in listing
    assert rem.cancel("маме") == 1
    assert rem.cancel() == 1
    assert rem.describe_pending() == "Активных напоминаний и таймеров нет."


def test_tools(rem):
    r = ToolRegistry()
    rem.register_tools(r)
    tool, args = r.validate("set_timer", {"seconds": 90})
    assert tool.handler(**args) == "Таймер на 1 минуту 30 секунд запущен."
    tool, args = r.validate("set_reminder", {"text": "чай", "minutes": 5})
    assert "через 5 минут" in tool.handler(**args)
    tool, args = r.validate("set_reminder", {"text": "чай"})
    assert "через сколько" in tool.handler(**args)
    tool, args = r.validate("cancel_reminders", {})
    assert tool.handler(**args) == "Отменено: 2."


def test_background_thread_delivers(rem):
    got = []
    fired = threading.Event()
    rem.add("x", 1)
    rem.clock_obj.t += 5
    rem.start(lambda text: (got.append(text), fired.set()), interval=0.01)
    assert fired.wait(2)
    rem.stop()
    assert got == ["Напоминание: x."]


def test_say_duration_nominative():
    assert say_duration(61, nominative=True) == "1 минута 1 секунда"
    assert say_duration(300, nominative=True) == "5 минут"
