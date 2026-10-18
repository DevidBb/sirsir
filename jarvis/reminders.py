"""Напоминания и таймеры.

Хранятся в той же SQLite-базе, что и память, поэтому переживают перезапуск.
Фоновый поток раз в секунду проверяет сроки и передаёт сработавшие
напоминания в обработчик (главный цикл произносит их, когда свободен).
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
import threading
import time
from typing import Callable

from .memory import Memory
from .security import normalize
from .tool_registry import Param, Tool, ToolRegistry

log = logging.getLogger(__name__)

MAX_PENDING = 50
MAX_DELAY_SECONDS = 7 * 24 * 3600

NUMBER_WORDS = {
    "ноль": 0, "одну": 1, "одна": 1, "один": 1, "две": 2, "два": 2, "три": 3, "четыре": 4,
    "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
    "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
    "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
    "девятнадцать": 19, "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
    "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80, "девяносто": 90,
    "сто": 100, "двести": 200, "триста": 300, "четыреста": 400, "пятьсот": 500,
    "шестьсот": 600, "семьсот": 700, "восемьсот": 800, "девятьсот": 900,
    # Косвенные падежи — для калькулятора: «15 процентов от двух тысяч», «трёх сотен».
    "одного": 1, "двух": 2, "трех": 3, "четырех": 4, "пяти": 5, "шести": 6, "семи": 7,
    "восьми": 8, "девяти": 9, "десяти": 10, "двадцати": 20, "тридцати": 30, "сорока": 40,
    "пятидесяти": 50, "ста": 100, "сотни": 100, "сотен": 100,
}
THOUSAND_WORDS = {"тысяча", "тысячи", "тысяч", "тысячу"}


def parse_number(text: str) -> int | None:
    """«5», «пять», «двадцать пять», «двести пятьдесят», «две тысячи» → int; иначе None."""
    text = text.strip().replace("ё", "е")
    if text.isdigit():
        return int(text)
    total, group, seen, last = 0, 0, False, 10**9
    for word in text.split():
        if word in THOUSAND_WORDS:
            if total:
                return None  # «тысяча тысяч»
            total, group, last = (group or 1) * 1000, 0, 1000
            seen = True
            continue
        value = NUMBER_WORDS.get(word)
        # Разряды идут по убыванию и не повторяются: «двадцать пять», но не «пять двадцать».
        if value is None or value >= last or (value < 10 and last < 20):
            return None
        group += value
        last = value if value >= 10 else 0
        seen = True
    return total + group if seen else None


def numbers_to_digits(text: str) -> str:
    """«двадцать пять» → «25» (пока подряд идут числительные)."""
    words, out, buf = text.split(), [], []
    for w in words + [""]:
        candidate = " ".join(buf + [w]) if w else None
        if candidate and parse_number(candidate) is not None and not w.isdigit():
            buf.append(w)
            continue
        if buf:
            out.append(str(parse_number(" ".join(buf))))
            buf = []
        if w and not w.isdigit() and parse_number(w) is not None:
            buf = [w]  # новое число: «семь тридцать» → «7 30»
        elif w:
            out.append(w)
    return " ".join(out)


_UNIT = r"(секунд[уы]?|сек|минут[уы]?|мин|час(?:а|ов)?)"
_NUM = r"(\d+|(?:[а-я]+ )?[а-я]+)"
DURATION_RE = re.compile(rf"^(?:на |через )?(?:{_NUM} )?{_UNIT}$")
COMPOUND_RE = re.compile(r"^(.+? (?:час(?:а|ов)?|минут[уы]?|мин)|час|полчаса|полтора часа) (?:и )?(.+)$")


def parse_duration(text: str) -> int | None:
    """«5 минут», «пять минут», «полчаса», «час», «полтора часа» → секунды."""
    t = normalize(text)
    t = re.sub(r"^(?:на|через) ", "", t)
    special = {"полчаса": 1800, "полтора часа": 5400, "час": 3600, "минуту": 60,
               "минутку": 60, "пару минут": 120, "полторы минуты": 90, "полминуты": 30}
    if t in special:
        return special[t]
    # Whisper пишет «1,5 минуты», «1.5 часа»; после нормализации — «1 5 минуты».
    m = re.fullmatch(r"(\d+) 5 (минуты|часа)", t)
    if m:
        return (int(m.group(1)) * 2 + 1) * (30 if m.group(2) == "минуты" else 1800)
    m = DURATION_RE.match(t)
    count = None
    if m:
        count = 1 if m.group(1) is None else parse_number(m.group(1))
    if count is None:
        # Составная длительность: «1 минуту 30 секунд», «час и 15 минут».
        parts = COMPOUND_RE.match(t)
        if parts:
            first, second = parse_duration(parts.group(1)), parse_duration(parts.group(2))
            if first and second and second < first:
                return first + second
        return None
    unit = m.group(2)
    mult = 1 if unit.startswith("сек") else 60 if unit.startswith("мин") else 3600
    return count * mult


def say_duration(seconds: int, nominative: bool = False) -> str:
    """«через 1 минуту» (винительный) или «осталась 1 минута» (именительный)."""
    def plural(n: int, one: str, few: str, many: str) -> str:
        if n % 10 == 1 and n % 100 != 11:
            return one
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return few
        return many

    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    parts = []
    if h:
        parts.append(f"{h} {plural(h, 'час', 'часа', 'часов')}")
    if m:
        parts.append(f"{m} {plural(m, 'минута' if nominative else 'минуту', 'минуты', 'минут')}")
    if s or not parts:
        parts.append(f"{s} {plural(s, 'секунда' if nominative else 'секунду', 'секунды', 'секунд')}")
    return " ".join(parts)


DAY_OFFSETS = {"сегодня": 0, "завтра": 1, "послезавтра": 2}


def next_time_of_day(hhmm: str, now: _dt.datetime) -> _dt.datetime | None:
    m = re.fullmatch(r"(\d{1,2})[:. ](\d{2})", hhmm.strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    due = now.replace(hour=h, minute=mi, second=0, microsecond=0)
    if due <= now:
        due += _dt.timedelta(days=1)
    return due


class Reminders:
    def __init__(self, memory: Memory, clock: Callable[[], float] = time.time):
        self.memory = memory
        self.clock = clock
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # --- хранение ---
    def add(self, text: str, delay_seconds: int, kind: str = "reminder") -> str:
        if delay_seconds <= 0:
            return "Время должно быть в будущем."
        if delay_seconds > MAX_DELAY_SECONDS:
            return "Можно поставить не дальше чем на неделю."
        if len(self.pending()) >= MAX_PENDING:
            return "Слишком много активных напоминаний."
        text = " ".join(text.split())[:200]
        self.memory.execute("INSERT INTO reminders(text, kind, due_at) VALUES(?, ?, ?)",
                            (text, kind, self.clock() + delay_seconds))
        if kind == "timer":
            return f"Таймер на {say_duration(delay_seconds)} запущен."
        return f"Напомню через {say_duration(delay_seconds)}: {text}."

    def add_at(self, text: str, hhmm: str, day: str | None = None) -> str:
        """day — «сегодня», «завтра», «послезавтра»; без него — ближайшее такое время."""
        now = _dt.datetime.fromtimestamp(self.clock())
        due = next_time_of_day(hhmm, now)
        if due is None:
            return f"Не понял время «{hhmm}»."
        if day:
            if day not in DAY_OFFSETS:
                return f"Не понял день «{day}»: скажите сегодня, завтра или послезавтра."
            date = now.date() + _dt.timedelta(days=DAY_OFFSETS[day])
            due = _dt.datetime.combine(date, due.time())
            if due <= now:
                return f"{day.capitalize()} в {due:%H:%M} уже прошло."
        result = self.add(text, int((due - now).total_seconds()))
        if result.startswith("Напомню"):
            offset = (due.date() - now.date()).days
            when = {0: "сегодня", 1: "завтра", 2: "послезавтра"}.get(offset, f"{due:%d.%m}")
            return f"Напомню {when} в {due:%H:%M}: {text}."
        return result

    def pending(self) -> list[tuple[int, str, str, float]]:
        return self.memory.execute(
            "SELECT id, text, kind, due_at FROM reminders WHERE done=0 ORDER BY due_at")

    def pop_due(self) -> list[tuple[str, str, float]]:
        now = self.clock()
        rows = self.memory.execute(
            "SELECT id, text, kind, due_at FROM reminders WHERE done=0 AND due_at<=? ORDER BY due_at",
            (now,))
        for rid, *_ in rows:
            self.memory.modify("UPDATE reminders SET done=1 WHERE id=?", (rid,))
        return [(text, kind, due) for _, text, kind, due in rows]

    def cancel(self, fragment: str = "") -> int:
        if fragment:
            return self.memory.modify("UPDATE reminders SET done=1 WHERE done=0 AND text LIKE ?",
                                      (f"%{fragment}%",))
        return self.memory.modify("UPDATE reminders SET done=1 WHERE done=0")

    def describe_pending(self) -> str:
        rows = self.pending()
        if not rows:
            return "Активных напоминаний и таймеров нет."
        now = self.clock()
        items = []
        for _, text, kind, due in rows[:10]:
            seconds = max(1, int(due - now))
            if kind == "timer":
                items.append(f"таймер, ещё {say_duration(seconds, nominative=True)}")
            else:
                items.append(f"{text} — через {say_duration(seconds)}")
        return f"Активно {len(rows)}: " + "; ".join(items) + "."

    @staticmethod
    def announcement(text: str, kind: str, due: float, now: float) -> str:
        late = now - due > 120  # пропущено, пока помощник был выключен
        prefix = "Пропущенное напоминание" if late else "Напоминание"
        if kind == "timer":
            return "Таймер сработал." if not late else "Пропущенный таймер сработал."
        return f"{prefix}: {text}."

    # --- фоновая проверка ---
    def start(self, on_due: Callable[[str], None], interval: float = 1.0) -> None:
        def loop() -> None:
            while not self._stop.wait(interval):
                try:
                    now = self.clock()
                    for text, kind, due in self.pop_due():
                        on_due(self.announcement(text, kind, due, now))
                except Exception:
                    if self._stop.is_set():
                        return  # база закрывается — штатное завершение
                    log.exception("Ошибка проверки напоминаний")

        self._stop.clear()
        self._thread = threading.Thread(target=loop, daemon=True, name="reminders")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._thread = None

    # --- инструменты ---
    def register_tools(self, registry: ToolRegistry) -> None:
        registry.register(Tool(
            "set_timer", "Поставить таймер",
            lambda minutes=0, seconds=0: self.add("таймер", int(minutes) * 60 + int(seconds), "timer"),
            {"minutes": Param("integer", "Минуты", required=False),
             "seconds": Param("integer", "Секунды", required=False)}))
        registry.register(Tool(
            "set_reminder",
            "Напомнить о чём-то через N минут или в указанное время HH:MM",
            self._tool_reminder,
            {"text": Param("string", "О чём напомнить", max_length=200),
             "minutes": Param("integer", "Через сколько минут", required=False),
             "at_time": Param("string", "Время в формате HH:MM", required=False, max_length=5),
             "day": Param("string", "День для at_time; без него — ближайшее такое время", required=False,
                          enum=list(DAY_OFFSETS))}))
        registry.register(Tool(
            "list_reminders", "Перечислить активные напоминания и таймеры",
            lambda: self.describe_pending()))
        registry.register(Tool(
            "cancel_reminders", "Отменить напоминания (по фрагменту текста) или все",
            lambda fragment="": self._tool_cancel(fragment),
            {"fragment": Param("string", "Фрагмент текста; пусто — все", required=False,
                               max_length=100)}))

    def _tool_reminder(self, text: str, minutes: int | None = None, at_time: str | None = None,
                       day: str | None = None) -> str:
        if at_time:
            return self.add_at(text, at_time, day)
        if minutes:
            return self.add(text, int(minutes) * 60)
        return "Скажите, через сколько минут или во сколько напомнить."

    def _tool_cancel(self, fragment: str) -> str:
        n = self.cancel(fragment)
        return f"Отменено: {n}." if n else "Нечего отменять."
