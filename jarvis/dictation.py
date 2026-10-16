"""Диктовка: ввод произнесённого текста в активное окно.

Безопасность:
- печатается распознанная речь пользователя; текст, сочинённый LLM, — только после
  голосового «подтверждаю» (см. assistant.py), сама модель печатать не может;
- Tab и управляющие символы в тексте не отправляются никогда;
- клавиши — только сочетания из белого списка HOTKEYS; Enter («отправь») — только
  после подтверждения;
- в окна терминалов (cmd, PowerShell, Windows Terminal и т.п.) ни ввод, ни клавиши
  не отправляются;
- используется SendInput с Unicode-символами, буфер обмена не затрагивается.
"""

from __future__ import annotations

import re
import sys
import time
from typing import Any, Callable

from .fuzzy import similarity
from .security import normalize

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


# Виртуальные клавиши Windows.
VK_SHIFT, VK_CONTROL, VK_RETURN = 0x10, 0x11, 0x0D
# Разрешённые сочетания клавиш: имя → (что сказать в журнал, клавиши по порядку нажатия).
HOTKEYS: dict[str, tuple[str, list[int]]] = {
    # Shift+Enter: перенос строки и в редакторах, и в мессенджерах (обычный Enter там отправляет).
    "new_line": ("новая строка", [VK_SHIFT, VK_RETURN]),
    "save": ("Ctrl+S", [VK_CONTROL, ord("S")]),
    "undo": ("Ctrl+Z", [VK_CONTROL, ord("Z")]),
    "select_all": ("Ctrl+A", [VK_CONTROL, ord("A")]),
    "copy": ("Ctrl+C", [VK_CONTROL, ord("C")]),
    "paste": ("Ctrl+V", [VK_CONTROL, ord("V")]),
    "close_tab": ("Ctrl+W", [VK_CONTROL, ord("W")]),
    "new_tab": ("Ctrl+T", [VK_CONTROL, ord("T")]),
    # Только после голосового подтверждения (assistant.py).
    "send": ("Enter", [VK_RETURN]),
}


def prepare_text(text: str, voice_punctuation: bool = True) -> str:
    """Голосовая пунктуация, без управляющих символов, с пробелом в конце."""
    text = " ".join(text.split())
    for pattern, repl in PUNCTUATION if voice_punctuation else []:
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


def _send_inputs(keys: list[tuple[int, int, int]]) -> None:
    """SendInput для списка (виртуальная клавиша, скан-код/символ, флаги)."""
    import ctypes
    from ctypes import wintypes

    INPUT_KEYBOARD = 1

    ULONG_PTR = ctypes.c_size_t

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _U(ctypes.Union):
        # Точное объединение из Windows API: размер INPUT должен совпасть с ожидаемым
        # SendInput (40 байт на x64, 28 на x86), иначе ввод молча отклоняется.
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    events = []
    for vk, scan, flags in keys:
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
        events.append(inp)
    arr = (INPUT * len(events))(*events)
    sent = ctypes.windll.user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))  # type: ignore
    if sent != len(events):
        raise OSError("Windows не принял ввод (окно с повышенными правами?)")


KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004


def _send_unicode(text: str) -> None:
    keys = []
    data = text.encode("utf-16-le")
    for i in range(0, len(data), 2):
        code = int.from_bytes(data[i:i + 2], "little")
        keys += [(0, code, KEYEVENTF_UNICODE), (0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
    _send_inputs(keys)


def _send_keys(vks: list[int]) -> None:
    """Сочетание клавиш: нажать по порядку, отпустить в обратном."""
    _send_inputs([(vk, 0, 0) for vk in vks] + [(vk, 0, KEYEVENTF_KEYUP) for vk in reversed(vks)])


# Сколько ждать после переключения окна, прежде чем печатать.
FOCUS_DELAY = 0.3
WINDOW_THRESHOLD = 0.75
MAX_TYPED_CHARS = 3000


class Dictation:
    def __init__(self, typer: Callable[[str], None] | None = None,
                 foreground: Callable[[], tuple[str, str]] | None = None,
                 key_sender: Callable[[list[int]], None] | None = None,
                 windows: Any = None, sleep: Callable[[float], None] = time.sleep):
        self.active = False
        self._typer = typer
        self._foreground = foreground
        self._key_sender = key_sender
        self._windows = windows  # WindowBackend: titles() и act(title, "focus")
        self._sleep = sleep

    def _check_target(self, fake: object) -> str | None:
        """Ошибка, если вводить некуда: не Windows или активно окно терминала."""
        if fake is None and sys.platform != "win32":
            return "Диктовка доступна только в Windows."
        cls, title = (self._foreground or _foreground_window)()
        if is_blocked_window(cls, title):
            return "В окно терминала печатать не буду."
        return None

    def type_text(self, text: str, voice_punctuation: bool = True) -> str | None:
        """Печатает текст; возвращает сообщение об ошибке или None.
        voice_punctuation=False — для готового текста («точка зрения» не станет «.»)."""
        prepared = prepare_text(text, voice_punctuation)[:MAX_TYPED_CHARS]
        if not prepared:
            return None
        error = self._check_target(self._typer)
        if error:
            return error
        try:
            (self._typer or _send_unicode)(prepared)
        except OSError as e:
            return f"Не получилось напечатать: {e}."
        return None

    def press(self, name: str) -> str | None:
        """Нажимает сочетание из белого списка HOTKEYS; возвращает ошибку или None."""
        if name not in HOTKEYS:
            return "Такое сочетание клавиш не разрешено."
        error = self._check_target(self._key_sender)
        if error:
            return error.replace("печатать", "нажимать клавиши")
        try:
            (self._key_sender or _send_keys)(HOTKEYS[name][1])
        except OSError as e:
            return f"Не получилось нажать клавиши: {e}."
        return None

    def _window_backend(self) -> Any:
        if self._windows is None:
            from .windows_control import WindowBackend
            self._windows = WindowBackend()
        return self._windows

    def find_window(self, query: str) -> str | None:
        """Заголовок открытого окна по подстроке или нечёткому совпадению."""
        backend = self._window_backend()
        if not getattr(backend, "available", True):
            return None
        titles = [t for t in backend.titles() if t.strip()]
        normalized_query = normalize(query)
        # Если название приложения — часть заголовка браузера («ChatGPT - Chrome»),
        # сначала ищем точное вхождение. Это надёжнее нечёткого сравнения коротких имён.
        contained = [t for t in titles if normalized_query and normalized_query in normalize(t)]
        if contained:
            return min(contained, key=lambda t: len(normalize(t)))
        scored = [(similarity(query, t), t) for t in titles]
        scored = [x for x in scored if x[0] >= WINDOW_THRESHOLD]
        return max(scored)[1] if scored else None

    def foreground_title(self) -> str:
        """Заголовок активного окна ('' — неизвестно или не Windows)."""
        if self._foreground is None and sys.platform != "win32":
            return ""
        try:
            return (self._foreground or _foreground_window)()[1]
        except Exception:
            return ""

    def wait_new_foreground(self, before: str, timeout: float = 8.0, step: float = 0.25) -> str | None:
        """Ждёт, пока активным станет другое окно (только что открытая программа)."""
        for _ in range(int(timeout / step)):
            title = self.foreground_title()
            if title and title != before:
                return title
            self._sleep(step)
        return None

    def type_into(self, window: str, text: str, voice_punctuation: bool = True) -> str | None:
        """«Напиши в телеграм привет»: переключиться на окно и напечатать.
        Название окна может быть из двух слов («google chrome …») — пробуем оба варианта."""
        words = text.split()
        title = None
        attempts = [(window, text)]
        if len(words) > 1:
            attempts.insert(0, (f"{window} {words[0]}", " ".join(words[1:])))
        for query, body in attempts:
            title = self.find_window(query)
            if title and (query == window or similarity(query, title) >= 0.9):
                text = body
                break
            title = None
        if title is None:
            return f"Окно «{window}» не найдено. Откройте программу и повторите."
        if is_blocked_window("", title):
            return "В окно терминала печатать не буду."
        try:
            self._window_backend().act(title, "focus")
        except Exception as e:  # pygetwindow бросает разное
            return f"Не получилось переключиться на окно {title}: {e}."
        self._sleep(FOCUS_DELAY)
        # Если Windows не отдал фокус, печатать нельзя: текст ушёл бы в чужое окно.
        if self._foreground is not None or sys.platform == "win32":
            _cls, current = (self._foreground or _foreground_window)()
            if normalize(current) != normalize(title):
                return f"Не получилось переключиться на окно {title}, печатать не стал."
        return self.type_text(text, voice_punctuation=voice_punctuation)
