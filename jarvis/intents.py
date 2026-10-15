"""Быстрые команды без LLM.

Частые простые фразы («открой блокнот», «который час») разбираются шаблонами
и сразу превращаются в вызов инструмента из реестра — это быстрее и надёжнее,
чем спрашивать модель. Всё, что не распознано, уходит в LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .calculator import phrase_to_expression
from .reminders import parse_duration
from .security import normalize


@dataclass
class Intent:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)


OPEN_RE = re.compile(r"^(?:пожалуйста )?(?:открой|запусти|включи|открыть|запустить)(?: мне)? (.+)$")
TIME_PHRASES = ("который час", "сколько времени", "какое время", "какое сегодня число",
                "какая сегодня дата", "какое число")
SYSINFO_PHRASES = ("состояние системы", "загрузка системы", "системная информация",
                   "как там компьютер", "сколько свободно памяти")
TIMER_RE = re.compile(r"^(?:поставь |заведи |запусти |включи )?таймер (.+)$")
REMIND_IN_RE = re.compile(r"^напомни(?: мне)? через (.+)$")
REMIND_AT_RE = re.compile(r"^напомни(?: мне)? в (\d{1,2}) (\d{2}) (?:что |чтобы |о том что )?(.+)$")
LIST_REMINDERS = ("какие напоминания", "мои напоминания", "какие таймеры", "список напоминаний",
                  "сколько осталось на таймере", "что с таймером")
CANCEL_REMINDERS_RE = re.compile(r"^(?:отмени|удали|сбрось|выключи|останови) (?:все )?(?:таймеры?|напоминания?)$")
POWER_PATTERNS = [
    (re.compile(r"^(?:заблокируй|заблокировать|блокируй) (?:компьютер|экран|пк)$"), "lock_screen"),
    (re.compile(r"^(?:выключи|выключить|отключи) (?:компьютер|пк)$"), "shutdown_computer"),
    (re.compile(r"^(?:перезагрузи|перезагрузить|перезапусти) (?:компьютер|пк)$"), "restart_computer"),
    (re.compile(r"^(?:переведи (?:компьютер |пк )?в (?:сон|спящий режим)|усыпи (?:компьютер|пк))$"),
     "sleep_computer"),
    (re.compile(r"^(?:отмени|отменить|останови) (?:выключение|перезагрузку)$"), "cancel_shutdown"),
]
NOTE_RE = re.compile(r"^(?:запиши|сделай|создай|добавь) (?:мне )?заметку (?:что |о том что )?(.+)$|^заметка (.+)$")
READ_NOTES = ("прочитай заметки", "какие у меня заметки", "мои заметки", "покажи заметки",
              "что в заметках", "прочитай мои заметки")
CALC_START = re.compile(r"^(?:сколько будет|посчитай|вычисли|корень|квадратный корень)\b")
MEDIA_PATTERNS = [
    (re.compile(r"(?:сделай )?(?:погромче|громче|прибавь звук|увеличь громкость)"), "volume_up"),
    (re.compile(r"(?:сделай )?(?:потише|тише|убавь звук|уменьши громкость)"), "volume_down"),
    (re.compile(r"(?:выключи|отключи|включи) звук|без звука"), "mute"),
    (re.compile(r"(?:пауза|поставь на паузу|продолжи воспроизведение|продолжи музыку)"), "play_pause"),
    (re.compile(r"(?:следующий трек|следующая песня|дальше трек)"), "next"),
    (re.compile(r"(?:предыдущий трек|предыдущая песня|прошлый трек)"), "previous"),
]


def _lookup(name: str, table: dict[str, str]) -> str | None:
    q = normalize(name)
    for key in table:
        if normalize(key) == q:
            return key
    return None


class IntentMatcher:
    def __init__(self, apps: dict[str, str], websites: dict[str, str],
                 folders: dict[str, str] | None = None):
        self.apps = apps
        self.websites = websites
        self.folders = folders or {}

    def match(self, text: str) -> Intent | None:
        norm = normalize(text)
        if not norm:
            return None
        reminder = self._match_reminder(norm)
        if reminder is not None:
            return reminder
        m = NOTE_RE.match(norm)
        if m:
            # Текст заметки берём из исходной фразы, чтобы сохранить регистр и знаки.
            body = m.group(1) or m.group(2)
            original = re.split(r"(?i)заметк[ау]\s*(?:что\s+|о том что\s+)?", text.strip(), maxsplit=1)
            note = original[1].strip(" ,:") if len(original) == 2 and original[1].strip() else body
            return Intent("add_note", {"text": note})
        if norm in READ_NOTES:
            return Intent("read_notes")
        if CALC_START.match(norm):
            expression = phrase_to_expression(text)
            if expression:
                return Intent("calculate", {"expression": expression})
        if len(norm.split()) > 7:
            return None  # длинные фразы — пусть разбирается LLM
        if norm in TIME_PHRASES:
            return Intent("current_time")
        if norm in SYSINFO_PHRASES:
            return Intent("system_info")
        for pattern, tool in POWER_PATTERNS:
            if pattern.match(norm):
                return Intent(tool)
        for pattern, action in MEDIA_PATTERNS:
            if pattern.fullmatch(norm):
                return Intent("media_control", {"action": action})
        m = OPEN_RE.match(norm)
        if m:
            target = re.sub(r"^(?:сайт|приложение|программу|папку) ", "", m.group(1)).strip()
            app = _lookup(target, self.apps)
            if app:
                return Intent("open_app", {"name": app})
            site = _lookup(target, self.websites)
            if site:
                return Intent("open_website", {"name": site})
            folder = _lookup(target, self.folders)
            if folder:
                return Intent("open_folder", {"name": folder})
        return None

    @staticmethod
    def _match_reminder(norm: str) -> Intent | None:
        if norm in LIST_REMINDERS:
            return Intent("list_reminders")
        if CANCEL_REMINDERS_RE.match(norm):
            return Intent("cancel_reminders")
        m = TIMER_RE.match(norm)
        if m:
            seconds = parse_duration(m.group(1))
            return Intent("set_timer", {"seconds": seconds}) if seconds else None
        m = REMIND_AT_RE.match(norm)
        if m:
            return Intent("set_reminder", {"text": m.group(3),
                                           "at_time": f"{int(m.group(1)):02d}:{m.group(2)}"})
        m = REMIND_IN_RE.match(norm)
        if m:
            # «через двадцать пять минут позвонить маме»: ищем самое длинное
            # начало, которое читается как длительность.
            words = m.group(1).split()
            for k in range(min(4, len(words) - 1), 0, -1):
                seconds = parse_duration(" ".join(words[:k]))
                if seconds:
                    rest = re.sub(r"^(?:что |чтобы |о том что |про )", "", " ".join(words[k:]) + " ").strip()
                    if rest and seconds % 60 == 0:
                        return Intent("set_reminder", {"text": rest, "minutes": seconds // 60})
            return None
        return None
