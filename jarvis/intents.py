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


OPEN_RE = re.compile(r"^(?:открой|запусти|включи|открыть|запустить|зайди на|перейди на)(?: мне)? (.+)$")
# Вежливые слова не меняют смысла команды: «открой, пожалуйста, блокнот».
POLITE_RE = re.compile(r"\b(?:пожалуйста|плиз)\b")
TIME_PHRASES = ("который час", "сколько времени", "какое время", "какое сегодня число",
                "какая сегодня дата", "какое число", "который сейчас час", "сколько сейчас времени",
                "сколько время", "сколько сейчас время", "скажи время", "подскажи время",
                "какой сегодня день", "какой сегодня день недели", "какой день недели",
                "какое сегодня число и день недели")
SYSINFO_PHRASES = ("состояние системы", "загрузка системы", "системная информация",
                   "как там компьютер", "сколько свободно памяти")
TIMER_RE = re.compile(r"^(?:поставь |заведи |запусти |включи )?таймер (.+)$|^засеки(?: таймер)? (.+)$")
# «Поставь будильник на 7:30» — Whisper пишет «7:30», после нормализации «7 30».
ALARM_RE = re.compile(r"^(?:поставь |заведи )?будильник (?:на )?(\d{1,2}) (\d{2})$|^разбуди меня в (\d{1,2}) (\d{2})$")
REMIND_IN_RE = re.compile(r"^напомни(?: мне)? через (.+)$")
REMIND_AT_RE = re.compile(r"^напомни(?: мне)? в (\d{1,2}) (\d{2}) (?:что |чтобы |о том что )?(.+)$")
LIST_REMINDERS = ("какие напоминания", "мои напоминания", "какие таймеры", "список напоминаний",
                  "сколько осталось на таймере", "что с таймером")
CANCEL_REMINDERS_RE = re.compile(r"^(?:отмени|удали|сбрось|выключи|останови) (?:все )?(?:таймеры?|напоминания?)$")
POWER_PATTERNS = [
    (re.compile(r"^(?:заблокируй|заблокировать|блокируй) (?:компьютер|комп|ноутбук|экран|пк)$"), "lock_screen"),
    (re.compile(r"^(?:выключи|выключить|отключи) (?:компьютер|комп|ноутбук|пк)$"), "shutdown_computer"),
    (re.compile(r"^(?:перезагрузи|перезагрузить|перезапусти) (?:компьютер|комп|ноутбук|пк)$"), "restart_computer"),
    (re.compile(r"^(?:переведи (?:компьютер |пк )?в (?:сон|спящий режим)|усыпи (?:компьютер|пк))$"),
     "sleep_computer"),
    (re.compile(r"^(?:отмени|отменить|останови) (?:выключение|перезагрузку)$"), "cancel_shutdown"),
]
NOTE_RE = re.compile(r"^(?:запиши|сделай|создай|добавь) (?:мне )?заметку (?:что |о том что )?(.+)$|^заметка (.+)$")
READ_NOTES = ("прочитай заметки", "какие у меня заметки", "мои заметки", "покажи заметки",
              "что в заметках", "прочитай мои заметки")
CALC_START = re.compile(r"^(?:сколько будет|посчитай|вычисли|корень|квадратный корень)\b")
MEDIA_PATTERNS = [
    (re.compile(r"(?:сделай )?(?:звук |музыку )?(?:по)?громче(?: звук| музыку)?"
                r"|(?:прибавь|увеличь|подними) (?:звук|громкость)|громкость (?:выше|больше)"), "volume_up"),
    (re.compile(r"(?:сделай )?(?:звук |музыку )?(?:по)?тише(?: звук| музыку)?"
                r"|(?:убавь|уменьши|понизь|снизь) (?:звук|громкость)|громкость (?:ниже|меньше)"), "volume_down"),
    (re.compile(r"(?:выключи|отключи|включи) звук|без звука"), "mute"),
    (re.compile(r"пауза|(?:поставь )?(?:музыку |видео )?на паузу|останови музыку"
                r"|(?:продолжи|возобнови) (?:воспроизведение|музыку)"), "play_pause"),
    (re.compile(r"(?:включи |поставь )?следующ(?:ий трек|ую песню|ая песня)|дальше трек"
                r"|(?:переключи|пропусти) (?:трек|песню)"), "next"),
    (re.compile(r"(?:включи |поставь |верни )?(?:предыдущ(?:ий трек|ую песню|ая песня)"
                r"|прошлый трек|прошлую песню)"), "previous"),
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
        norm = normalize(POLITE_RE.sub(" ", normalize(text)))
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
            seconds = parse_duration(m.group(1) or m.group(2))
            return Intent("set_timer", {"seconds": seconds}) if seconds else None
        m = ALARM_RE.match(norm)
        if m:
            hour, minute = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            if int(hour) <= 23 and int(minute) <= 59:
                return Intent("set_reminder", {"text": "Будильник", "at_time": f"{int(hour):02d}:{minute}"})
            return None
        m = REMIND_AT_RE.match(norm)
        if m:
            return Intent("set_reminder", {"text": m.group(3),
                                           "at_time": f"{int(m.group(1)):02d}:{m.group(2)}"})
        m = REMIND_IN_RE.match(norm)
        if m:
            # «через двадцать пять минут позвонить маме»: ищем самое длинное
            # начало, которое читается как длительность.
            words = m.group(1).split()
            for k in range(min(6, len(words) - 1), 0, -1):
                seconds = parse_duration(" ".join(words[:k]))
                if seconds:
                    rest = re.sub(r"^(?:что |чтобы |о том что |про )", "", " ".join(words[k:]) + " ").strip()
                    if rest and seconds % 60 == 0:
                        return Intent("set_reminder", {"text": rest, "minutes": seconds // 60})
                    # «через 1 минуту 30 секунд ...» — не отрезаем секунды в текст, пусть решает LLM.
                    return None
            return None
        return None
