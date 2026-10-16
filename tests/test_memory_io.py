"""Экспорт и импорт памяти: перенос, повторы, фильтр секретов, безопасные настройки."""

import json
import time

import pytest

from jarvis.memory import Memory
from jarvis.memory_io import export_memory, import_memory, summary
from jarvis.schedules import Scheduler


def filled():
    mem = Memory(":memory:")
    mem.add_fact("меня зовут Давид")
    mem.add_fact("отвечай тремя пунктами", "preference")
    mem.execute("INSERT INTO notes(text, created_at) VALUES(?, ?)", ("купить молоко", 1.0))
    mem.add_task("сравни python", "3.13 быстрее", ["internet_search"])
    mem.execute("INSERT INTO reminders(text, kind, due_at) VALUES(?, 'reminder', ?)", ("позвонить", time.time() + 3600))
    mem.set_setting("tts.rate", 200)
    mem.set_setting("files.allowed_dirs", ["C:/"])  # не голосовая настройка — не экспортируется
    sched = Scheduler(mem)
    sched.add(prompt="сводка новостей", at_time="09:00", days="weekdays")
    return mem, sched


def test_roundtrip_and_duplicates():
    mem, sched = filled()
    data = json.loads(json.dumps(export_memory(mem, sched), ensure_ascii=False))
    assert data["format"] == "jarvis-memory" and "files.allowed_dirs" not in data["settings"]
    target, target_sched = Memory(":memory:"), None
    target_sched = Scheduler(target)
    stats = import_memory(target, data, target_sched)
    assert stats == {"facts": 2, "notes": 1, "tasks": 1, "reminders": 1, "schedules": 1, "settings": 1, "skipped": 0}
    assert {f["kind"] for f in target.list_facts()} == {"fact", "preference"}
    assert target_sched.list()[0].enabled is False  # плановые задачи — выключены
    assert target.get_setting("tts.rate") == 200
    again = import_memory(target, data, target_sched)
    assert again["skipped"] == 6 and again["facts"] == 0 and len(target_sched.list()) == 1
    assert "Ничего нового" in summary({k: 0 for k in again} | {"skipped": 0})


def test_import_filters_unsafe_content():
    target = Memory(":memory:")
    data = {"format": "jarvis-memory", "version": 1,
            "facts": [{"text": "мой пароль: Qwerty12345!"}, {"text": 42}, "мусор", {"text": "люблю чай", "kind": "admin"}],
            "notes": [{"text": "api key sk-ant-abcdefghijklmnopqrstuvwxyz"}],
            "reminders": [{"text": "старое", "due_at": 1.0}],
            "settings": {"security.allowed_hosts": ["evil.example"], "tts.rate": 99999, "llm.provider": "claude"}}
    stats = import_memory(target, data)
    assert stats["facts"] == 1 and target.list_facts()[0]["kind"] == "fact"
    assert stats["notes"] == 0 and stats["reminders"] == 0
    assert target.all_settings() == {"llm.provider": "claude"}


@pytest.mark.parametrize("bad", [[], {"format": "other"}, {"format": "jarvis-memory", "version": 99},
                                 {"format": "jarvis-memory", "version": 1, "facts": "строка"}])
def test_import_rejects_wrong_files(bad):
    with pytest.raises(ValueError):
        import_memory(Memory(":memory:"), bad)
