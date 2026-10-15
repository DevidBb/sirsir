"""Быстрые команды без LLM.

Частые простые фразы («открой блокнот», «который час») разбираются шаблонами
и сразу превращаются в вызов инструмента из реестра — это быстрее и надёжнее,
чем спрашивать модель. Всё, что не распознано, уходит в LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

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
    def __init__(self, apps: dict[str, str], websites: dict[str, str]):
        self.apps = apps
        self.websites = websites

    def match(self, text: str) -> Intent | None:
        norm = normalize(text)
        if not norm or len(norm.split()) > 7:
            return None  # длинные фразы — пусть разбирается LLM
        if norm in TIME_PHRASES:
            return Intent("current_time")
        if norm in SYSINFO_PHRASES:
            return Intent("system_info")
        for pattern, action in MEDIA_PATTERNS:
            if pattern.fullmatch(norm):
                return Intent("media_control", {"action": action})
        m = OPEN_RE.match(norm)
        if m:
            target = re.sub(r"^(?:сайт|приложение|программу) ", "", m.group(1)).strip()
            app = _lookup(target, self.apps)
            if app:
                return Intent("open_app", {"name": app})
            site = _lookup(target, self.websites)
            if site:
                return Intent("open_website", {"name": site})
        return None
