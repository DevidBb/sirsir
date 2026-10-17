"""Гибридный режим: куда уходит фраза (scripts/benchmark_routing.py — те же случаи таблицей)."""

import datetime as dt
import importlib.util

import pytest

from conftest import ROOT, FakeLLM, text
from jarvis.assistant import Reply, hybrid_route, is_followup, needs_fresh_data

spec = importlib.util.spec_from_file_location("benchmark_routing", ROOT / "scripts" / "benchmark_routing.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


def test_every_benchmark_phrase_goes_where_expected():
    rows = bench.route_cases()
    wrong = [f"{r['text']}: {r['route']} вместо {r['expected']}" for r in rows if r["route"] != r["expected"]]
    assert not wrong, "\n".join(wrong)
    assert len(rows) >= 100


@pytest.mark.parametrize("query,last,target", [
    ("а подробнее?", "local", "local"),
    ("а подробнее?", "claude", "claude"),
    ("а подробнее?", None, "claude"),            # без прошлого ответа «подробно» — длинный разбор
    ("давай ещё один", "local", "local"),
    ("почему?", "claude", "claude"),
    ("почему небо голубое", "claude", "local"),  # новый вопрос, а не продолжение
    ("а какая сейчас погода?", "local", "claude"),  # свежие данные важнее продолжения
])
def test_followups_stay_with_the_previous_model(query, last, target):
    assert hybrid_route(query, last)[0] == target


def test_personal_time_words_are_not_fresh_data():
    assert not needs_fresh_data("что мне сегодня приготовить")
    assert not needs_fresh_data("я сейчас устал, предложи занятие")
    assert needs_fresh_data("какая сегодня погода")
    assert needs_fresh_data("мне сегодня нужен зонт?")  # «зонт» — вопрос о погоде
    assert not is_followup("сравни iPhone 17 и Samsung S26 и дай подробный вывод")


def test_route_is_shown_and_followup_window_expires(config):
    from jarvis.main import App
    config.set("browser.enabled", False)
    config.set("llm.provider", "hybrid")
    app = App(config, voice=False)
    a = app.assistant
    a.llm = FakeLLM([text("Рекурсия — это…"), text("Подробнее: …"), text("ок")])
    calls = []

    class Claude:
        session_id = None

        def run(self, assistant, request, on_sentence=None, image=None):
            calls.append(request)
            return Reply("Ответ Claude.")

        def close(self, wait=0):
            pass
    a.agent_backend = Claude()
    events = []
    a.on_event = events.append
    now = [1000.0]
    a.clock = lambda: now[0]
    try:
        a.handle("что такое рекурсия")
        a.handle("а подробнее?")
        assert calls == []
        assert [e["reason"] for e in events if e["type"] == "route"] == ["короткий вопрос",
                                                                        "продолжение разговора"]
        now[0] += 3600  # через час это уже не продолжение: «подробно» — длинный разбор, Claude
        a.handle("расскажи подробнее")
        assert calls == ["расскажи подробнее"]
        a.handle("новый диалог")
        assert a._last_route is None
    finally:
        app.close()


def test_reminder_with_day_and_part_of_day():
    from jarvis.intents import IntentMatcher
    m = IntentMatcher({}, {})
    assert m.match("напомни завтра в 9 позвонить врачу").args == {
        "text": "позвонить врачу", "at_time": "09:00", "day": "завтра"}
    assert m.match("напомни в 7 30 вечера купить хлеб").args == {"text": "купить хлеб", "at_time": "19:30"}
    assert m.match("напомни мне в 18:05 что выключить плиту").args == {
        "text": "выключить плиту", "at_time": "18:05"}
    assert m.match("напомни в 12 ночи проверить почту").args["at_time"] == "00:00"
    assert m.match("напомни в 25 00 что-то") is None or m.match("напомни в 25 00 что-то").tool != "set_reminder"


def test_reminder_day_offsets(tmp_path):
    from jarvis.memory import Memory
    from jarvis.reminders import Reminders
    now = dt.datetime(2026, 10, 17, 8, 0).timestamp()
    r = Reminders(Memory(tmp_path / "m.db"), clock=lambda: now)
    assert r.add_at("позвонить врачу", "09:00", "завтра") == "Напомню завтра в 09:00: позвонить врачу."
    assert r.add_at("кофе", "09:00") == "Напомню сегодня в 09:00: кофе."
    assert r.add_at("поздно", "07:00", "сегодня") == "Сегодня в 07:00 уже прошло."
    assert r.add_at("x", "10:00", "в среду").startswith("Не понял день")
    assert r.add_at("послезавтра", "10:00", "послезавтра") == "Напомню послезавтра в 10:00: послезавтра."


def test_power_phrase():
    from jarvis.calculator import calculate, phrase_to_expression
    assert calculate(phrase_to_expression("сколько будет 2 в 10 степени")) == "Получается 1024."
    assert calculate(phrase_to_expression("3 в степени 4")) == "Получается 81."
