"""Нечёткое сравнение названий по-русски и по-английски.

«телеграм» ≈ «Telegram», «стим» ≈ «Steam», «фотошоп» ≈ «Adobe Photoshop 2024»:
русское название переводится в латиницу (транслитерация + словарь частых
программ), затем сравнивается со словами названия.
"""

from __future__ import annotations

import difflib

from .security import normalize

TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
    "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

# Как программы называют вслух → как они называются в меню «Пуск».
ALIASES = {
    "телеграм": "telegram", "телеграмм": "telegram", "телега": "telegram",
    "стим": "steam", "фотошоп": "photoshop", "дискорд": "discord",
    "хром": "chrome", "гугл хром": "google chrome", "ворд": "word", "эксель": "excel",
    "ексель": "excel", "пауэрпоинт": "powerpoint", "повер поинт": "powerpoint",
    "павер поинт": "powerpoint", "аутлук": "outlook", "скайп": "skype", "зум": "zoom",
    "спотифай": "spotify", "ватсап": "whatsapp", "вотсап": "whatsapp", "вацап": "whatsapp",
    "вайбер": "viber", "обс": "obs", "вс код": "visual studio code",
    "визуал студио код": "visual studio code", "визуал студио": "visual studio",
    "файрфокс": "firefox", "фаерфокс": "firefox", "мозилла": "firefox", "опера": "opera",
    "яндекс браузер": "yandex", "эдж": "edge", "влс": "vlc", "эпик геймс": "epic games",
    "эпик": "epic games", "пейнт": "paint", "паинт": "paint", "блокнот": "notepad",
    "калькулятор": "calculator", "ноушен": "notion", "обсидиан": "obsidian",
    "торрент": "torrent", "ютор": "utorrent", "винрар": "winrar", "севен зип": "7 zip",
    "аймп": "aimp", "пайчарм": "pycharm", "тимс": "teams", "майнкрафт": "minecraft",
    "блендер": "blender", "гимп": "gimp", "акробат": "acrobat", "кейпасс": "keepass",
    "анидеск": "anydesk", "тимвьюер": "teamviewer", "кьюбейс": "cubase",
}


def translit(text: str) -> str:
    return "".join(TRANSLIT.get(ch, ch) for ch in text)


def variants(text: str) -> set[str]:
    """Нормализованный текст, его латинская запись и замена по словарю."""
    norm = normalize(text)
    out = {norm, translit(norm)}
    alias = norm
    for ru, en in sorted(ALIASES.items(), key=lambda kv: -len(kv[0])):
        alias = f" {alias} ".replace(f" {ru} ", f" {en} ").strip()
    out.add(alias)
    out.add(translit(alias))
    return {v for v in out if v}


def _score(q: str, n: str) -> float:
    if q == n:
        return 1.0
    if f" {q} " in f" {n} ":
        return 0.92  # «photoshop» в «adobe photoshop 2024»
    qw, nw = q.split(), n.split()
    best = difflib.SequenceMatcher(None, q, n).ratio()
    for i in range(len(nw)):
        window = " ".join(nw[i:i + len(qw)])
        best = max(best, difflib.SequenceMatcher(None, q, window).ratio() * 0.9)
    return best


def similarity(query: str, name: str) -> float:
    """0…1: насколько произнесённое название похоже на название программы/файла."""
    names = {normalize(name), translit(normalize(name))}
    return max((_score(q, n) for q in variants(query) for n in names if n), default=0.0)
