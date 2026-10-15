"""Проверка Windows API вживую (для GitHub Actions и для ПК пользователя).

Ничего не удаляет и не выключает: запускает Блокнот и закрывает его,
нажимает мультимедиа-клавишу «громче/тише», пишет и удаляет тестовый файл
автозапуска во временной папке.

    python scripts/windows_smoke.py
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis import console_utf8  # noqa: E402

console_utf8()

CHECKS: list[tuple[str, object]] = []


def check(name):
    def wrap(fn):
        CHECKS.append((name, fn))  # только регистрируем; выполняет main()
        return fn
    return wrap


@check("структура INPUT для SendInput (диктовка)")
def _():
    from ctypes import wintypes
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
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]
    expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
    assert ctypes.sizeof(INPUT) == expected, ctypes.sizeof(INPUT)
    return f"sizeof(INPUT) = {ctypes.sizeof(INPUT)}"


@check("окно переднего плана (защита диктовки от терминалов)")
def _():
    from jarvis.dictation import _foreground_window, is_blocked_window
    cls, title = _foreground_window()
    return f"класс={cls!r}, заголовок={title!r}, блокируется={is_blocked_window(cls, title)}"


@check("меню «Пуск»: ярлыки и разбор .lnk")
def _():
    from jarvis.app_finder import StartMenuIndex, parse_lnk, unsafe_target_reason
    index = StartMenuIndex()
    shortcuts = index.shortcuts()
    assert shortcuts, f"ярлыков нет в {index.dirs}"
    links = [s for s in shortcuts if s.path.suffix.lower() == ".lnk"]
    parsed = [(s.name, *parse_lnk(s.path.read_bytes())) for s in links]
    resolved = [p for p in parsed if p[1]]
    refused = [p[0] for p in parsed if unsafe_target_reason(p[1], p[2])]
    sample = "; ".join(f"{n} → {t}" for n, t, _ in resolved[:3])
    return (f"ярлыков {len(shortcuts)}, цель прочитана у {len(resolved)} из {len(links)}, "
            f"отклонено {len(refused)}. Примеры: {sample}")


@check("голоса SAPI (pyttsx3)")
def _():
    import pyttsx3
    engine = pyttsx3.init()
    voices = [v.name for v in engine.getProperty("voices") or []]
    assert voices, "нет ни одного голоса"
    return ", ".join(voices)


@check("автозапуск: запись и удаление JARVIS.cmd")
def _():
    from jarvis import autostart
    with tempfile.TemporaryDirectory() as tmp:
        path = autostart.enable(ROOT, Path(tmp))
        data = path.read_bytes()
        assert b"\r\r\n" not in data and b"\r\n" in data, data
        assert autostart.disable(Path(tmp))
    return f"папка автозагрузки: {autostart.startup_dir()}"


@check("запуск Блокнота, поиск окна, свернуть, закрыть")
def _():
    import pygetwindow
    from jarvis.config import Config
    from jarvis.windows_control import WindowBackend, WindowsController
    ctl = WindowsController(Config.load(ROOT / "config.toml"))
    msg = ctl.open_app("блокнот")
    win = None
    for _ in range(40):
        time.sleep(0.25)
        wins = [w for w in pygetwindow.getAllWindows() if "блокнот" in w.title.lower()
                or "notepad" in w.title.lower()]
        if wins:
            win = wins[0]
            break
    assert win is not None, f"окно Блокнота не появилось ({msg})"
    backend = WindowBackend()
    title = win.title.split(" - ")[-1].split(" — ")[-1]
    out = [msg, ctl.window_action(title, "minimize"), ctl.window_action(title, "restore"),
           ctl.window_action(title, "focus")]
    ctl.window_action(title, "close")
    time.sleep(1)
    left = [w for w in pygetwindow.getAllWindows() if w.title == win.title]
    if left:
        subprocess.run(["taskkill", "/IM", "notepad.exe", "/F"], capture_output=True)
    assert backend.available
    return " | ".join(out)


@check("мультимедиа-клавиши (громче, затем тише)")
def _():
    from jarvis.windows_control import MEDIA_KEYS, _default_key_press
    _default_key_press(MEDIA_KEYS["volume_up"])
    _default_key_press(MEDIA_KEYS["volume_down"])
    return "ok"


@check("системная информация")
def _():
    from jarvis.config import Config
    from jarvis.windows_control import WindowsController
    return WindowsController(Config.load(ROOT / "config.toml")).system_info()


@check("пути к DLL CUDA из pip-пакетов nvidia")
def _():
    from jarvis.speech_recognition import add_nvidia_dll_dirs
    added = add_nvidia_dll_dirs()
    return f"добавлено: {len(added)}"


@check("ctranslate2 (движок Whisper) импортируется")
def _():
    import ctranslate2
    return f"версия {ctranslate2.__version__}, CUDA-устройств: {ctranslate2.get_cuda_device_count()}"


@check("Vosk импортируется")
def _():
    import vosk
    vosk.SetLogLevel(-1)
    return "ok"


@check("sounddevice / PortAudio")
def _():
    import sounddevice as sd
    devices = sd.query_devices()
    return f"устройств: {len(devices)} (на виртуальной машине их может не быть)"


def main() -> int:
    print(f"Python {sys.version.split()[0]}, {sys.platform}, {os.environ.get('PROCESSOR_IDENTIFIER', '')}\n")
    results: list[tuple[str, bool, str]] = []
    for name, fn in CHECKS:
        try:
            results.append((name, True, str(fn() or "")))
        except Exception as e:
            results.append((name, False, f"{type(e).__name__}: {e}"))
            traceback.print_exc()
    for name, ok, detail in results:
        print(f"[{'OK  ' if ok else 'FAIL'}] {name}: {detail}")
    failed = [r for r in results if not r[1]]
    print(f"\nНе пройдено: {len(failed)} из {len(results)}" if failed else "\nВсё прошло.")
    return 1 if failed else 0


if __name__ == "__main__":
    if sys.platform != "win32":
        print("Этот скрипт проверяет Windows API и запускается только в Windows.")
        sys.exit(0)
    sys.exit(main())
