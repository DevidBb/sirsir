"""Плановые задачи: «каждое утро в 9:00 сделай сводку новостей».

Задача — это текст запроса и расписание: каждый день, по будням, по выходным, по выбранным
дням недели, раз в N минут (не чаще раза в 15 минут) или один раз в указанную дату.
Хранится в SQLite (memory.db) вместе с журналом запусков.

Задача выполняется без пользователя, поэтому действия, требующие подтверждения
(отправка, удаление, изменение файлов, новая плановая задача…), в ней не выполняются —
модель получает отказ и сообщает о нём в итоге. Создать задачу голосом или через модель
можно только с подтверждением; из HUD — щелчком самого пользователя. Если компьютер был
выключен, пропущенный запуск выполняется, только если опоздание меньше часа.
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .memory import Memory
from .security import looks_secret
from .tool_registry import Param, Tool, ToolRegistry, ToolResult

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt        TEXT NOT NULL,
    at_time       TEXT NOT NULL DEFAULT '',
    days          TEXT NOT NULL DEFAULT 'daily',
    every_minutes INTEGER NOT NULL DEFAULT 0,
    once_date     TEXT NOT NULL DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 1,
    next_run      REAL,
    created_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS schedule_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER NOT NULL,
    started_at  REAL NOT NULL,
    status      TEXT NOT NULL,
    result      TEXT NOT NULL DEFAULT ''
);
"""

MAX_SCHEDULES = 20
MIN_INTERVAL = 15
MAX_PROMPT = 500
GRACE_SECONDS = 3600
KEEP_RUNS = 300
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_ALIASES = {
    "пн": "mon", "понедельник": "mon", "вт": "tue", "вторник": "tue", "ср": "wed", "среда": "wed",
    "чт": "thu", "четверг": "thu", "пт": "fri", "пятница": "fri", "сб": "sat", "суббота": "sat",
    "вс": "sun", "воскресенье": "sun",
}
DAY_NAMES = {"mon": "пн", "tue": "вт", "wed": "ср", "thu": "чт", "fri": "пт", "sat": "сб", "sun": "вс"}
UNATTENDED_NOTE = ("Это плановая задача: пользователя сейчас нет рядом. Сделай запрос сам и "
                   "закончи коротким итогом. Действия, требующие подтверждения, не выполняются — "
                   "если такое понадобилось, напиши об этом в итоге.\n\nЗапрос: ")


class ScheduleError(ValueError):
    pass


@dataclass
class Schedule:
    id: int
    prompt: str
    at_time: str
    days: str
    every_minutes: int
    once_date: str
    enabled: bool
    next_run: float | None
    created_at: float

    def describe(self) -> str:
        if self.every_minutes:
            return f"каждые {self.every_minutes} мин"
        if self.once_date:
            return f"{self.once_date} в {self.at_time}"
        days = {"daily": "каждый день", "weekdays": "по будням", "weekends": "по выходным"}.get(
            self.days) or ", ".join(DAY_NAMES.get(d, d) for d in self.days.split(","))
        return f"{days} в {self.at_time}"


def normalize_days(days: str) -> str:
    value = (days or "daily").strip().lower()
    if value in ("daily", "каждый день", "ежедневно", ""):
        return "daily"
    if value in ("weekdays", "будни", "по будням"):
        return "weekdays"
    if value in ("weekends", "выходные", "по выходным"):
        return "weekends"
    parts = []
    for item in re.split(r"[,\s]+", value):
        if not item:
            continue
        code = DAY_ALIASES.get(item, item[:3])
        if code not in WEEKDAYS:
            raise ScheduleError(f"непонятный день недели: {item}")
        if code not in parts:
            parts.append(code)
    return ",".join(sorted(parts, key=WEEKDAYS.index))


def parse_time(value: str) -> str:
    m = re.fullmatch(r"(\d{1,2})[:. ](\d{2})", (value or "").strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ScheduleError("время — в формате ЧЧ:ММ, например 09:00")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _day_allowed(days: str, day: _dt.date) -> bool:
    code = WEEKDAYS[day.weekday()]
    if days == "daily":
        return True
    if days == "weekdays":
        return day.weekday() < 5
    if days == "weekends":
        return day.weekday() >= 5
    return code in days.split(",")


def compute_next(s: Schedule, after: _dt.datetime) -> _dt.datetime | None:
    """Следующий запуск строго после ``after`` (локальное время) или None (разовая прошла)."""
    if s.every_minutes:
        return after + _dt.timedelta(minutes=s.every_minutes)
    h, m = (int(x) for x in s.at_time.split(":"))
    if s.once_date:
        due = _dt.datetime.combine(_dt.date.fromisoformat(s.once_date), _dt.time(h, m))
        return due if due > after else None
    day = after.date()
    for _ in range(8):
        due = _dt.datetime.combine(day, _dt.time(h, m))
        if due > after and _day_allowed(s.days, day):
            return due
        day += _dt.timedelta(days=1)
    return None


class Scheduler:
    def __init__(self, memory: Memory, clock: Callable[[], float] = time.time) -> None:
        self.memory = memory
        self.clock = clock
        for statement in SCHEMA.split(";"):
            if statement.strip():
                memory.execute(statement)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.running: int | None = None

    def _now(self) -> _dt.datetime:
        return _dt.datetime.fromtimestamp(self.clock())

    # --- хранение ---
    def _row(self, r: tuple) -> Schedule:
        return Schedule(r[0], r[1], r[2], r[3], r[4], r[5], bool(r[6]), r[7], r[8])

    def list(self) -> list[Schedule]:
        return [self._row(r) for r in self.memory.execute(
            "SELECT id, prompt, at_time, days, every_minutes, once_date, enabled, next_run, created_at "
            "FROM schedules ORDER BY id")]

    def get(self, sid: int) -> Schedule | None:
        return next((s for s in self.list() if s.id == sid), None)

    def validate(self, prompt: str, at_time: str = "", days: str = "", every_minutes: int = 0,
                 date: str = "") -> dict[str, Any]:
        prompt = " ".join((prompt or "").split())
        if not prompt:
            raise ScheduleError("не указано, что делать")
        if len(prompt) > MAX_PROMPT:
            raise ScheduleError(f"запрос длиннее {MAX_PROMPT} символов")
        if looks_secret(prompt):
            raise ScheduleError("в запросе похоже есть пароль или ключ — такое не сохраняю")
        if len(self.list()) >= MAX_SCHEDULES:
            raise ScheduleError(f"уже {MAX_SCHEDULES} задач — удалите ненужные")
        every = int(every_minutes or 0)
        if every:
            if every < MIN_INTERVAL:
                raise ScheduleError(f"не чаще раза в {MIN_INTERVAL} минут")
            return {"prompt": prompt, "at_time": "", "days": "daily", "every_minutes": every, "once_date": ""}
        at = parse_time(at_time)
        once = ""
        if date:
            try:
                once = _dt.date.fromisoformat(date.strip()).isoformat()
            except ValueError as e:
                raise ScheduleError("дата — в формате ГГГГ-ММ-ДД") from e
        fields = {"prompt": prompt, "at_time": at, "days": normalize_days(days) if not once else "daily",
                  "every_minutes": 0, "once_date": once}
        probe = Schedule(0, prompt, at, fields["days"], 0, once, True, None, 0)
        if compute_next(probe, self._now()) is None:
            raise ScheduleError("это время уже прошло")
        return fields

    def add(self, **kwargs: Any) -> Schedule:
        f = self.validate(**kwargs)
        probe = Schedule(0, f["prompt"], f["at_time"], f["days"], f["every_minutes"], f["once_date"], True, None, 0)
        nxt = compute_next(probe, self._now())
        self.memory.execute(
            "INSERT INTO schedules (prompt, at_time, days, every_minutes, once_date, enabled, next_run, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (f["prompt"], f["at_time"], f["days"], f["every_minutes"], f["once_date"],
             nxt.timestamp() if nxt else None, self.clock()))
        sid = self.memory.execute("SELECT max(id) FROM schedules")[0][0]
        return self.get(sid)

    def set_enabled(self, sid: int, enabled: bool) -> bool:
        s = self.get(sid)
        if s is None:
            return False
        nxt = compute_next(s, self._now()) if enabled else None
        self.memory.execute("UPDATE schedules SET enabled = ?, next_run = ? WHERE id = ?",
                            (int(enabled and nxt is not None), nxt.timestamp() if nxt else None, sid))
        return True

    def delete(self, sid: int) -> bool:
        if self.get(sid) is None:
            return False
        self.memory.execute("DELETE FROM schedules WHERE id = ?", (sid,))
        self.memory.execute("DELETE FROM schedule_runs WHERE schedule_id = ?", (sid,))
        return True

    def log_run(self, sid: int, status: str, result: str) -> None:
        self.memory.execute("INSERT INTO schedule_runs (schedule_id, started_at, status, result) VALUES (?, ?, ?, ?)",
                            (sid, self.clock(), status, result[:2000]))
        self.memory.execute("DELETE FROM schedule_runs WHERE id NOT IN "
                            "(SELECT id FROM schedule_runs ORDER BY id DESC LIMIT ?)", (KEEP_RUNS,))

    def runs(self, sid: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
        where, params = ("WHERE schedule_id = ?", (sid, limit)) if sid is not None else ("", (limit,))
        return [{"id": r[0], "schedule_id": r[1], "started_at": r[2], "status": r[3], "result": r[4]}
                for r in self.memory.execute(
                    f"SELECT id, schedule_id, started_at, status, result FROM schedule_runs {where} "
                    "ORDER BY id DESC LIMIT ?", params)]

    def describe(self) -> list[dict[str, Any]]:
        out = []
        for s in self.list():
            last = self.runs(s.id, 1)
            out.append({"id": s.id, "prompt": s.prompt, "when": s.describe(), "enabled": s.enabled,
                        "next_run": s.next_run, "running": self.running == s.id,
                        "last": last[0] if last else None})
        return out

    # --- запуск ---
    def take_due(self) -> list[Schedule]:
        """Наступившие задачи; следующий запуск переносится сразу, чтобы не запустить дважды."""
        now = self._now()
        due = []
        for s in self.list():
            if not s.enabled or s.next_run is None or s.next_run > now.timestamp():
                continue
            late = now.timestamp() - s.next_run
            nxt = compute_next(s, now)
            self.memory.execute("UPDATE schedules SET next_run = ?, enabled = ? WHERE id = ?",
                                (nxt.timestamp() if nxt else None, int(nxt is not None), s.id))
            if late > GRACE_SECONDS:
                self.log_run(s.id, "skipped", f"пропущено: компьютер был выключен или Джарвис не работал "
                                              f"(опоздание {int(late // 60)} мин)")
                continue
            due.append(s)
        return due

    def start(self, on_due: Callable[[Schedule], None], interval: float = 20.0) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.wait(interval):
                try:
                    for s in self.take_due():
                        on_due(s)
                except Exception:  # noqa: BLE001
                    if self._stop.is_set():
                        return
                    log.exception("Ошибка планировщика")
        self._stop.clear()
        self._thread = threading.Thread(target=loop, daemon=True, name="schedules")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._thread = None

    def execute(self, s: Schedule, assistant: Any) -> str:
        """Выполнить задачу без пользователя и записать итог в журнал."""
        self.running = s.id
        try:
            reply = assistant.run_unattended(s.prompt)
            text = reply.text or "Готово."
            status = "ok" if reply.ok and reply.action != "stop" else ("stopped" if reply.ok else "error")
        except Exception as e:  # noqa: BLE001
            log.exception("Плановая задача %s", s.id)
            text, status = f"Ошибка: {e}", "error"
        finally:
            self.running = None
        self.log_run(s.id, status, text)
        return text

    # --- инструменты ---
    def register_tools(self, registry: ToolRegistry) -> None:
        def schedule_task(prompt: str, time: str = "", days: str = "", every_minutes: int = 0,
                          date: str = "") -> ToolResult:
            try:
                s = self.add(prompt=prompt, at_time=time, days=days, every_minutes=every_minutes, date=date)
            except ScheduleError as e:
                return ToolResult(f"Не запланировал: {e}.", ok=False)
            return ToolResult(f"Запланировал задачу №{s.id}: «{s.prompt}» — {s.describe()}.",
                              {"kind": "schedule", "id": s.id})

        def confirm(args: dict[str, Any]) -> str:
            try:
                f = self.validate(args.get("prompt", ""), args.get("time", ""), args.get("days", ""),
                                  args.get("every_minutes", 0), args.get("date", ""))
            except ScheduleError:
                return "создать плановую задачу"
            probe = Schedule(0, f["prompt"], f["at_time"], f["days"], f["every_minutes"], f["once_date"], True, None, 0)
            return f"запланировать «{f['prompt']}» — {probe.describe()}"

        def precheck(args: dict[str, Any]) -> str | None:
            try:
                self.validate(args.get("prompt", ""), args.get("time", ""), args.get("days", ""),
                              args.get("every_minutes", 0), args.get("date", ""))
            except ScheduleError as e:
                return f"Не запланировал: {e}."
            return None

        def list_schedules() -> ToolResult:
            items = self.describe()
            if not items:
                return ToolResult("Плановых задач нет.", {"kind": "schedules", "count": 0})
            lines = [f"№{i['id']}: «{i['prompt']}» — {i['when']}" + ("" if i["enabled"] else " (выключена)")
                     for i in items]
            return ToolResult("Плановые задачи:\n" + "\n".join(lines), {"kind": "schedules", "count": len(items)})

        def cancel_schedule(id: int) -> ToolResult:
            return (ToolResult(f"Удалил плановую задачу №{id}.") if self.delete(id)
                    else ToolResult(f"Задачи №{id} нет.", ok=False))

        registry.register(Tool(
            "schedule_task", "Создать плановую задачу: что сделать (prompt) и когда — time ЧЧ:ММ и days "
            "(daily, weekdays, weekends или дни: mon,wed), или every_minutes (от 15), или date ГГГГ-ММ-ДД "
            "для разового запуска. Нужно подтверждение пользователя.", schedule_task,
            {"prompt": Param("string", "Что сделать, как просьба пользователя", max_length=MAX_PROMPT),
             "time": Param("string", "Время ЧЧ:ММ", required=False, max_length=5),
             "days": Param("string", "daily, weekdays, weekends или mon,tue,…", required=False, max_length=40),
             "every_minutes": Param("integer", f"Интервал в минутах (от {MIN_INTERVAL})", required=False),
             "date": Param("string", "Дата ГГГГ-ММ-ДД для разовой задачи", required=False, max_length=10)},
            dangerous=True, precheck=precheck, confirm_check=confirm, confirm_timeout=60, category="schedule"))
        registry.register(Tool("list_schedules", "Список плановых задач", list_schedules, category="schedule"))
        registry.register(Tool("cancel_schedule", "Удалить плановую задачу по номеру", cancel_schedule,
                               {"id": Param("integer", "Номер задачи")}, dangerous=True,
                               confirm_template="удалить плановую задачу №{id}", category="schedule"))
