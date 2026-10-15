"""Диктовка: ввод произнесённого текста в активное окно.

Безопасность:
- печатается только распознанная речь пользователя, LLM к этому доступа не имеет;
- Enter, Tab и управляющие символы не отправляются никогда;
- в окна терминалов (cmd, PowerShell, Windows Terminal и т.п.) ввод запрещён;
- используется SendInput с Unicode-символами, буфер обмена не затрагивается.
"""

from __future__ import annotations

import re
import sys
from typing import Callable

# Классы окон консолей и терминалов, куда печатать нельзя.
BLOCKED_WINDOW_CLASSES = {
    "consolewindowclass",                 # cmd.exe, powershell.exe
    "cascadia_hosting_window_class",      # Windows Terminal
    "mintty",                             # Git Bash
    "virtualconsoleclass",                # ConEmu
    "putty",
}
BLOCKED_TITLE_WORDS = ("powershell", "command prompt", "командная строка", "terminal", "терминал",
                       "cmd.exe", "bash", "wsl")

# Голосовая пунктуация.
PUNCTUATION = [
    (r"\s*\bвосклицательный знак\b", "!"),
    (r"\s*\bвопросительный знак\b", "?"),
    (r"\s*\bточка с запятой\b", ";"),
    (r"\s*\bдвоеточие\b", ":"),
    (r"\s*\bзапятая\b", ","),
    (r"\s*\bточка\b", "."),
    (r"\s*\bтире\b", " —"),
    (r"\bоткрыть скобку\s*", "("),
    (r"\s*\bзакрыть скобку\b", ")"),
]


def prepare_text(text: str) -> str:
    """Голосовая пунктуация, без управляющих символов, с пробелом в конце."""
    text = " ".join(text.split())
    for pattern, repl in PUNCTUATION:
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)
    text = "".join(ch for ch in text if ch >= " " and ch != "\x7f")
    text = re.sub(r"\s+([,.!?;:)])", r"\1", text)
    return text.strip() + " " if text.strip() else ""


def is_blocked_window(window_class: str, title: str) -> bool:
    cls = window_class.lower()
    t = title.lower()
    return cls in BLOCKED_WINDOW_CLASSES or any(w in t for w in BLOCKED_TITLE_WORDS)


def _foreground_window() -> tuple[str, str]:
    import ctypes
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    hwnd = user32.GetForegroundWindow()
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    length = user32.GetWindowTextLengthW(hwnd)
    title = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, title, length + 1)
    return cls.value, title.value


def _send_unicode(text: str) -> None:
    import ctypes
    from ctypes import wintypes

    INPUT_KEYBOARD = 1
    KEYEVENTF_UNICODE = 0x0004
    KEYEVENTF_KEYUP = 0x0002

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _U(ctypes.Union):
        # Самый большой член объединения — MOUSEINPUT; задаём размер с запасом.
        _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    events = []
    data = text.encode("utf-16-le")
    for i in range(0, len(data), 2):
        code = int.from_bytes(data[i:i + 2], "little")
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            inp = INPUT(type=INPUT_KEYBOARD)
            inp.u.ki = KEYBDINPUT(0, code, flags, 0, 0)
            events.append(inp)
    arr = (INPUT * len(events))(*events)
    sent = ctypes.windll.user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))  # type: ignore
    if sent != len(events):
        raise OSError("Windows не принял ввод (окно с повышенными правами?)")


class Dictation:
    def __init__(self, typer: Callable[[str], None] | None = None,
                 foreground: Callable[[], tuple[str, str]] | None = None):
        self.active = False
        self._typer = typer
        self._foreground = foreground

    def type_text(self, text: str) -> str | None:
        """Печатает текст; возвращает сообщение об ошибке или None."""
        prepared = prepare_text(text)
        if not prepared:
            return None
        if self._typer is None and sys.platform != "win32":
            return "Диктовка доступна только в Windows."
        foreground = self._foreground or _foreground_window
        cls, title = foreground()
        if is_blocked_window(cls, title):
            return "В окно терминала печатать не буду."
        try:
            (self._typer or _send_unicode)(prepared)
        except OSError as e:
            return f"Не получилось напечатать: {e}."
        return None
