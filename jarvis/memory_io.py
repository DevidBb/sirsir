"""Экспорт и импорт памяти Джарвиса (JSON): перенос на другой компьютер, резервная копия.

В файл попадают факты и предпочтения, заметки, итоги задач, активные напоминания,
плановые задачи и голосовые настройки. Секретов в памяти нет (они туда не записываются),
при импорте записи ещё раз проверяются фильтром секретов.

Импорт только добавляет: существующие записи не удаляются и не заменяются, повторы
пропускаются. Плановые задачи импортируются выключенными — включить их нужно самому
(иначе чужой файл мог бы запланировать действия). Из настроек берутся только голосовые
параметры в допустимых пределах и выбор модели; настройки безопасности, папки и сеть
через импорт не меняются.
"""

from __future__ import annotations

import time
from typing import Any

from .memory import FACT_KINDS, VOICE_SETTINGS, Memory
from .security import looks_secret

FORMAT = "jarvis-memory"
VERSION = 1
MAX_ITEMS = 5000


def export_memory(memory: Memory, scheduler: Any = None) -> dict[str, Any]:
    reminders = [{"text": r[0], "kind": r[1], "due_at": r[2]} for r in memory.execute(
        "SELECT text, kind, due_at FROM reminders WHERE done = 0 ORDER BY due_at")]
    notes = [{"text": r[0], "created_at": r[1]} for r in memory.execute(
        "SELECT text, created_at FROM notes ORDER BY id")]
    data: dict[str, Any] = {
        "format": FORMAT, "version": VERSION, "exported_at": time.time(),
        "facts": [{"text": f["text"], "kind": f["kind"], "created_at": f["created_at"]}
                  for f in reversed(memory.list_facts())],
        "notes": notes,
        "tasks": [{"request": t["request"], "result": t["result"], "tools": t["tools"],
                   "created_at": t["created_at"]} for t in reversed(memory.list_tasks(10_000))],
        "reminders": reminders,
        "settings": {k: v for k, v in memory.all_settings().items() if _setting_ok(k, v)},
        "schedules": [],
    }
    if scheduler is not None:
        data["schedules"] = [{"prompt": s.prompt, "at_time": s.at_time, "days": s.days,
                              "every_minutes": s.every_minutes, "once_date": s.once_date,
                              "enabled": s.enabled} for s in scheduler.list()]
    return data


def _setting_ok(key: str, value: Any) -> bool:
    if key in VOICE_SETTINGS:
        kind, lo, hi, _ = VOICE_SETTINGS[key]
        return isinstance(value, (int, float)) and not isinstance(value, bool) and lo <= value <= hi
    if key == "llm.provider":
        from .providers import PROVIDERS
        return value in PROVIDERS
    if key == "tts.silero_speaker":
        from .text_to_speech import SILERO_VOICES
        return value in SILERO_VOICES
    return False


def _text(item: Any, key: str, limit: int) -> str | None:
    value = item.get(key) if isinstance(item, dict) else None
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())[:limit]
    return value if value and not looks_secret(value) else None


def _number(item: dict, key: str, default: float) -> float:
    value = item.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def import_memory(memory: Memory, data: Any, scheduler: Any = None) -> dict[str, int]:
    """Добавить записи из экспорта. Возвращает счётчики добавленного и пропущенного."""
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError("это не файл памяти Джарвиса")
    if not isinstance(data.get("version"), int) or data["version"] > VERSION:
        raise ValueError("файл от более новой версии Джарвиса")
    stats = {"facts": 0, "notes": 0, "tasks": 0, "reminders": 0, "schedules": 0, "settings": 0, "skipped": 0}

    def items(key: str) -> list:
        value = data.get(key) or []
        if not isinstance(value, list):
            raise ValueError(f"{key}: ожидается список")
        if len(value) > MAX_ITEMS:
            raise ValueError(f"{key}: больше {MAX_ITEMS} записей")
        return value

    sections = {k: items(k) for k in ("facts", "notes", "tasks", "reminders", "schedules")}

    for f in sections["facts"]:
        text = _text(f, "text", 300)
        kind = f.get("kind") if isinstance(f, dict) and f.get("kind") in FACT_KINDS else "fact"
        if text and memory.add_fact(text, kind):
            stats["facts"] += 1
        else:
            stats["skipped"] += 1

    existing_notes = {r[0] for r in memory.execute("SELECT text FROM notes")}
    for n in sections["notes"]:
        text = _text(n, "text", 500)
        if not text or text in existing_notes:
            stats["skipped"] += 1
            continue
        memory.execute("INSERT INTO notes(text, created_at) VALUES(?, ?)", (text, _number(n, "created_at", time.time())))
        existing_notes.add(text)
        stats["notes"] += 1

    existing_tasks = {(r[0], r[1]) for r in memory.execute("SELECT request, result FROM task_log")}
    for t in sections["tasks"]:
        request, result = _text(t, "request", 300), _text(t, "result", 500)
        tools = t.get("tools", "") if isinstance(t, dict) and isinstance(t.get("tools", ""), str) else ""
        if not request or not result or (request, result) in existing_tasks:
            stats["skipped"] += 1
            continue
        memory.execute("INSERT INTO task_log(request, result, tools, created_at) VALUES(?, ?, ?, ?)",
                       (request, result, tools[:300], _number(t, "created_at", time.time())))
        existing_tasks.add((request, result))
        stats["tasks"] += 1

    now = time.time()
    existing_rem = {(r[0], r[1]) for r in memory.execute("SELECT text, due_at FROM reminders WHERE done = 0")}
    for r in sections["reminders"]:
        text = _text(r, "text", 200)
        due = _number(r, "due_at", 0) if isinstance(r, dict) else 0
        kind = r.get("kind") if isinstance(r, dict) and r.get("kind") in ("reminder", "timer") else "reminder"
        if not text or due <= now or (text, due) in existing_rem:  # прошедшие не переносим
            stats["skipped"] += 1
            continue
        memory.execute("INSERT INTO reminders(text, kind, due_at) VALUES(?, ?, ?)", (text, kind, due))
        stats["reminders"] += 1

    if scheduler is not None:
        from .schedules import Schedule, ScheduleError
        existing = {(s.prompt, s.describe()) for s in scheduler.list()}
        for s in sections["schedules"]:
            if not isinstance(s, dict):
                stats["skipped"] += 1
                continue
            try:
                f = scheduler.validate(str(s.get("prompt", "")), str(s.get("at_time", "")), str(s.get("days", "")),
                                       int(s.get("every_minutes") or 0), str(s.get("once_date", "")))
            except (ScheduleError, ValueError, TypeError):
                stats["skipped"] += 1
                continue
            probe = Schedule(0, f["prompt"], f["at_time"], f["days"], f["every_minutes"], f["once_date"],
                             False, None, 0)
            if (probe.prompt, probe.describe()) in existing:
                stats["skipped"] += 1
                continue
            item = scheduler.add(prompt=f["prompt"], at_time=f["at_time"], days=f["days"],
                                 every_minutes=f["every_minutes"], date=f["once_date"])
            scheduler.set_enabled(item.id, False)  # включает пользователь сам
            existing.add((probe.prompt, probe.describe()))
            stats["schedules"] += 1
    else:
        stats["skipped"] += len(sections["schedules"])

    settings = data.get("settings") or {}
    if isinstance(settings, dict):
        for key, value in settings.items():
            if _setting_ok(str(key), value):
                memory.set_setting(str(key), value)
                stats["settings"] += 1
            else:
                stats["skipped"] += 1
    return stats


def summary(stats: dict[str, int]) -> str:
    names = {"facts": "фактов и предпочтений", "notes": "заметок", "tasks": "итогов задач",
             "reminders": "напоминаний", "schedules": "плановых задач (выключены)", "settings": "настроек"}
    parts = [f"{names[k]}: {v}" for k, v in stats.items() if k in names and v]
    text = "Добавлено — " + ", ".join(parts) if parts else "Ничего нового не добавлено"
    return text + (f". Пропущено (повторы, прошедшие, неподходящие): {stats['skipped']}." if stats["skipped"] else ".")
