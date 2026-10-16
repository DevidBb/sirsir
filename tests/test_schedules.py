"""Плановые задачи: расписание, пропуски, выполнение без подтверждений, изоляция диалога."""

import datetime as dt

import pytest

from conftest import FakeLLM
from jarvis.assistant import Assistant, Reply
from jarvis.memory import Memory
from jarvis.schedules import Schedule, ScheduleError, Scheduler, compute_next, normalize_days
from jarvis.tool_registry import Tool, ToolRegistry


def ts(*args):
    return dt.datetime(*args).timestamp()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def sched(**kw):
    base = dict(id=1, prompt="p", at_time="09:00", days="daily", every_minutes=0, once_date="",
                enabled=True, next_run=None, created_at=0)
    base.update(kw)
    return Schedule(**base)


def test_compute_next():
    wed = dt.datetime(2026, 10, 14, 10, 0)  # среда
    assert compute_next(sched(), wed) == dt.datetime(2026, 10, 15, 9, 0)
    assert compute_next(sched(), wed.replace(hour=8)) == dt.datetime(2026, 10, 14, 9, 0)
    assert compute_next(sched(days="weekends"), wed) == dt.datetime(2026, 10, 17, 9, 0)
    fri = dt.datetime(2026, 10, 16, 10, 0)
    assert compute_next(sched(days="weekdays"), fri) == dt.datetime(2026, 10, 19, 9, 0)
    assert compute_next(sched(days="mon,wed"), wed) == dt.datetime(2026, 10, 19, 9, 0)
    assert compute_next(sched(every_minutes=30, at_time=""), wed) == wed + dt.timedelta(minutes=30)
    assert compute_next(sched(once_date="2026-10-14"), wed) is None
    assert normalize_days("пн, ср") == "mon,wed" and normalize_days("по будням") == "weekdays"
    with pytest.raises(ScheduleError):
        normalize_days("someday")


def test_validation():
    s = Scheduler(Memory(":memory:"), Clock(ts(2026, 10, 14, 10, 0)))
    for kwargs, msg in [({"prompt": ""}, "что делать"), ({"prompt": "x", "at_time": "25:00"}, "ЧЧ:ММ"),
                        ({"prompt": "x", "every_minutes": 5}, "15"),
                        ({"prompt": "x", "at_time": "09:00", "date": "2026-10-13"}, "прошло"),
                        ({"prompt": "войди с паролем: hunter2Secret!", "at_time": "09:00"}, "парол")]:
        with pytest.raises(ScheduleError, match=msg):
            s.add(**kwargs)


def test_due_and_missed_runs():
    clock = Clock(ts(2026, 10, 14, 8, 0))
    s = Scheduler(Memory(":memory:"), clock)
    item = s.add(prompt="сводка новостей", at_time="09:00")
    assert item.next_run == ts(2026, 10, 14, 9, 0) and s.take_due() == []
    clock.t = ts(2026, 10, 14, 9, 0, 30)
    assert [d.id for d in s.take_due()] == [item.id]
    assert s.take_due() == []  # не запускается дважды
    assert s.get(item.id).next_run == ts(2026, 10, 15, 9, 0)
    clock.t = ts(2026, 10, 15, 13, 0)  # компьютер был выключен утром
    assert s.take_due() == [] and s.runs(item.id)[0]["status"] == "skipped"
    assert s.get(item.id).next_run == ts(2026, 10, 16, 9, 0)


def test_once_disables_after_run_and_toggle():
    clock = Clock(ts(2026, 10, 14, 8, 0))
    s = Scheduler(Memory(":memory:"), clock)
    item = s.add(prompt="разово", at_time="09:00", date="2026-10-14")
    clock.t = ts(2026, 10, 14, 9, 1)
    assert len(s.take_due()) == 1 and s.get(item.id).enabled is False
    other = s.add(prompt="ежедневно", at_time="10:00")
    assert s.set_enabled(other.id, False) and s.get(other.id).next_run is None
    assert s.set_enabled(other.id, True) and s.get(other.id).next_run == ts(2026, 10, 14, 10, 0)
    assert s.delete(other.id) and s.get(other.id) is None


@pytest.fixture
def env(config):
    registry = ToolRegistry()
    calls = []
    registry.register(Tool("lookup", "Найти", lambda: calls.append("lookup") or "нашёл"))
    registry.register(Tool("send", "Отправить", lambda: calls.append("send") or "отправлено",
                           dangerous=True, confirm_template="отправить письмо"))
    memory = Memory(":memory:")
    scheduler = Scheduler(memory, Clock(ts(2026, 10, 14, 8, 0)))
    scheduler.register_tools(registry)
    return registry, memory, scheduler, calls, config


def call(name):
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": {}}}]}


def test_unattended_run_refuses_confirmations_and_isolates_dialog(env):
    registry, memory, scheduler, calls, config = env
    llm = FakeLLM([{"role": "assistant", "content": "Привет!"},
                   call("lookup"), call("send"), {"role": "assistant", "content": "Нашёл; письмо не отправил."}])
    assistant = Assistant(config, llm, registry, memory)
    assistant.handle("расскажи что-нибудь про погоду на марсе")
    before = assistant.conversation.messages("")
    item = scheduler.add(prompt="найди и отправь отчёт", at_time="09:00")
    text = scheduler.execute(item, assistant)
    assert text == "Нашёл; письмо не отправил." and calls == ["lookup"]
    assert not assistant.confirmations.has_pending() and assistant.unattended is False
    assert assistant.conversation.messages("") == before  # диалог пользователя не тронут
    prompt = llm.requests[1]["messages"][-1]["content"]
    assert "плановая задача" in prompt and prompt.endswith("найди и отправь отчёт")
    tool_msgs = [m for m in llm.requests[3]["messages"] if m.get("role") == "tool"]
    assert "требует подтверждения" in tool_msgs[-1]["content"]
    assert scheduler.runs(item.id)[0]["status"] == "ok"


def test_model_error_logged_as_error(env):
    from jarvis.llm import LLMError
    registry, memory, scheduler, _, config = env
    assistant = Assistant(config, FakeLLM([LLMError("нет связи")]), registry, memory)
    item = scheduler.add(prompt="сводка", at_time="09:00")
    scheduler.execute(item, assistant)
    assert scheduler.runs(item.id)[0]["status"] == "error"


def test_schedule_tool_needs_confirmation(env):
    registry, memory, scheduler, _, config = env
    assistant = Assistant(config, FakeLLM([]), registry, memory)
    reply = assistant._run_tool("schedule_task", {"prompt": "сводка новостей", "time": "9:00", "days": "weekdays"})
    assert isinstance(reply, Reply) and "запланировать «сводка новостей» — по будням в 09:00" in reply.text
    assert scheduler.list() == []
    assert "Запланировал задачу №1" in assistant.handle("подтверждаю").text
    bad = assistant._run_tool("schedule_task", {"prompt": "x", "every_minutes": 1})
    assert not isinstance(bad, Reply) and "15" in bad
    assert "№1" in assistant._run_tool("list_schedules", {})
    # В плановой задаче новую не создать: нужно подтверждение.
    assistant.unattended = True
    assert "требует подтверждения" in assistant._run_tool("schedule_task", {"prompt": "ещё", "time": "10:00"})
