"""Безопасное управление Windows.

Всё, что можно сделать, явно перечислено: приложения и сайты берутся только из
config.toml, поиск и удаление файлов ограничены разрешёнными каталогами,
команды не проходят через оболочку (никакого shell=True / PowerShell).
"""

from __future__ import annotations

import datetime as _dt
import os
import platform
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .security import normalize
from .tool_registry import Param, Tool, ToolRegistry

IS_WINDOWS = sys.platform == "win32"


# Виртуальные мультимедиа-клавиши Windows.
MEDIA_KEYS = {
    "volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD,
    "play_pause": 0xB3, "next": 0xB0, "previous": 0xB1,
}
MEDIA_NAMES = {
    "volume_up": "Громче", "volume_down": "Тише", "mute": "Переключаю звук",
    "play_pause": "Пауза или воспроизведение", "next": "Следующий трек", "previous": "Предыдущий трек",
}


def _default_key_press(vk: int) -> None:
    if not IS_WINDOWS:
        raise OSError("мультимедиа-клавиши доступны только в Windows")
    import ctypes
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    KEYEVENTF_KEYUP = 0x0002
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def _default_launcher(command: str) -> None:
    if IS_WINDOWS:
        # ShellExecute находит программы через App Paths (msedge.exe и т.п.)
        # и не интерпретирует строку как командную строку.
        os.startfile(command)  # type: ignore[attr-defined]
    else:
        subprocess.Popen([command], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class WindowBackend:
    """Управление окнами через pygetwindow (только Windows)."""

    def __init__(self) -> None:
        try:
            import pygetwindow  # type: ignore
            self._gw = pygetwindow
        except Exception:
            self._gw = None

    @property
    def available(self) -> bool:
        return self._gw is not None

    def titles(self) -> list[str]:
        if not self._gw:
            return []
        return [t for t in self._gw.getAllTitles() if t.strip()]

    def find(self, query: str):
        if not self._gw:
            return None
        q = normalize(query)
        for win in self._gw.getAllWindows():
            if win.title.strip() and q in normalize(win.title):
                return win
        return None

    def act(self, query: str, action: str) -> str | None:
        win = self.find(query)
        if win is None:
            return None
        if action == "focus":
            if win.isMinimized:
                win.restore()
            win.activate()
        elif action == "minimize":
            win.minimize()
        elif action == "maximize":
            win.maximize()
        elif action == "restore":
            win.restore()
        elif action == "close":
            win.close()
        return win.title


class WindowsController:
    def __init__(self, config: Config,
                 launcher: Callable[[str], None] | None = None,
                 url_opener: Callable[[str], Any] | None = None,
                 window_backend: WindowBackend | None = None,
                 trash: Callable[[str], None] | None = None,
                 key_press: Callable[[int], None] | None = None):
        self.apps: dict[str, str] = config.get("windows.apps", {})
        self.websites: dict[str, str] = config.get("windows.websites", {})
        self.allowed_dirs = [Path(d).expanduser().resolve()
                             for d in config.get("windows.search.allowed_dirs", [])]
        self.max_results = int(config.get("windows.search.max_results", 10))
        self.max_scanned = int(config.get("windows.search.max_scanned_files", 20000))
        self.launch = launcher or _default_launcher
        self.open_url = url_opener or webbrowser.open
        self.windows = window_backend or WindowBackend()
        self._trash = trash
        self.key_press = key_press or _default_key_press

    # --- поиск по белым спискам ---
    @staticmethod
    def _lookup(name: str, table: dict[str, str]) -> tuple[str, str] | None:
        q = normalize(name)
        for key, value in table.items():
            if normalize(key) == q:
                return key, value
        for key, value in table.items():
            k = normalize(key)
            if k in q or q in k:
                return key, value
        return None

    # --- приложения и сайты ---
    def open_app(self, name: str) -> str:
        found = self._lookup(name, self.apps)
        if not found:
            return f"Приложение «{name}» не в списке разрешённых. Доступны: {', '.join(self.apps)}."
        key, command = found
        self.launch(command)
        return f"Запускаю {key}."

    def open_website(self, name: str) -> str:
        found = self._lookup(name, self.websites)
        if not found:
            return f"Сайт «{name}» не в списке разрешённых. Доступны: {', '.join(self.websites)}."
        key, url = found
        if not url.startswith(("https://", "http://")):
            return f"Адрес сайта {key} в настройках некорректен."
        self.open_url(url)
        return f"Открываю {key}."

    # --- файлы ---
    def _inside_allowed(self, path: Path) -> bool:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            return False
        return any(resolved == d or resolved.is_relative_to(d) for d in self.allowed_dirs)

    def search_files(self, query: str) -> str:
        q = normalize(query)
        if len(q) < 2:
            return "Слишком короткий запрос для поиска."
        results: list[Path] = []
        scanned = 0
        for base in self.allowed_dirs:
            if not base.is_dir():
                continue
            for root, dirs, files in os.walk(base, followlinks=False):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for fname in files:
                    scanned += 1
                    if q in normalize(fname):
                        results.append(Path(root) / fname)
                        if len(results) >= self.max_results:
                            break
                    if scanned >= self.max_scanned:
                        break
                if len(results) >= self.max_results or scanned >= self.max_scanned:
                    break
        if not results:
            return f"Файлов по запросу «{query}» не найдено."
        listing = "; ".join(str(p) for p in results)
        return f"Найдено {len(results)}: {listing}"

    def delete_file(self, path: str) -> str:
        p = Path(path).expanduser()
        if not self._inside_allowed(p):
            return "Удалять можно только файлы в разрешённых каталогах."
        if not p.is_file():
            return f"Файл {path} не найден."
        trash = self._trash
        if trash is None:
            try:
                from send2trash import send2trash  # type: ignore
                trash = send2trash
            except ImportError:
                return "Модуль send2trash не установлен, удаление отключено."
        trash(str(p.resolve()))
        return f"Файл {p.name} перемещён в корзину."

    # --- система ---
    def system_info(self) -> str:
        from .text_normalize import plural
        gb = ("гигабайт", "гигабайта", "гигабайт")
        parts = [f"Система {platform.system()} {platform.release()}"]
        try:
            import psutil  # type: ignore
            parts.append(f"процессор загружен на {psutil.cpu_percent(interval=0.3):.0f}%")
            vm = psutil.virtual_memory()
            total = round(vm.total / 2**30)
            parts.append(f"память занята на {vm.percent:.0f}%, всего {total} {plural(total, gb)}")
            battery = psutil.sensors_battery()
            if battery is not None:
                parts.append(f"заряд батареи {battery.percent:.0f}%")
        except ImportError:
            pass
        free = int(shutil.disk_usage(Path.home().anchor or "/").free / 2**30)
        parts.append(f"свободно на диске {free} {plural(free, gb)}")
        return ", ".join(parts) + "."

    @staticmethod
    def current_time(now: _dt.datetime | None = None) -> str:
        now = now or _dt.datetime.now()
        months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
                  "августа", "сентября", "октября", "ноября", "декабря"]
        days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
        return f"Сейчас {now:%H:%M}, {now.day} {months[now.month - 1]}, {days[now.weekday()]}."

    # --- звук и музыка ---
    def media_control(self, action: str, steps: int = 1) -> str:
        vk = MEDIA_KEYS[action]
        repeat = max(1, min(int(steps), 10)) if action in ("volume_up", "volume_down") else 1
        try:
            for _ in range(repeat * (2 if action in ("volume_up", "volume_down") else 1)):
                self.key_press(vk)  # одно нажатие громкости = 2%, шаг = 4%
        except OSError as e:
            return f"Не получилось: {e}."
        return MEDIA_NAMES[action] + "."

    # --- окна ---
    def list_windows(self) -> str:
        if not self.windows.available:
            return "Управление окнами недоступно: нужен пакет pygetwindow на Windows."
        titles = self.windows.titles()[:15]
        return "Открытые окна: " + "; ".join(titles) if titles else "Открытых окон нет."

    def window_action(self, title: str, action: str) -> str:
        if not self.windows.available:
            return "Управление окнами недоступно: нужен пакет pygetwindow на Windows."
        done = self.windows.act(title, action)
        if done is None:
            return f"Окно «{title}» не найдено."
        names = {"focus": "Переключаюсь на", "minimize": "Сворачиваю", "maximize": "Разворачиваю",
                 "restore": "Восстанавливаю", "close": "Закрываю"}
        return f"{names[action]} окно {done}."

    # --- регистрация инструментов ---
    def register_tools(self, registry: ToolRegistry) -> None:
        registry.register(Tool(
            "open_app", "Запустить приложение из списка разрешённых: " + ", ".join(self.apps),
            lambda name: self.open_app(name),
            {"name": Param("string", "Название приложения")}))
        registry.register(Tool(
            "open_website", "Открыть сайт из списка разрешённых: " + ", ".join(self.websites),
            lambda name: self.open_website(name),
            {"name": Param("string", "Название сайта")}))
        registry.register(Tool(
            "search_files", "Найти файлы по части имени в папках пользователя",
            lambda query: self.search_files(query),
            {"query": Param("string", "Часть имени файла", max_length=100)}))
        registry.register(Tool(
            "delete_file", "Переместить файл в корзину (только в разрешённых папках)",
            lambda path: self.delete_file(path),
            {"path": Param("string", "Полный путь к файлу", max_length=400)},
            dangerous=True, confirm_template="переместить в корзину файл {path}"))
        registry.register(Tool(
            "system_info", "Сообщить загрузку процессора, памяти, диска и батареи",
            lambda: self.system_info()))
        registry.register(Tool(
            "current_time", "Сообщить текущее время и дату", lambda: self.current_time()))
        registry.register(Tool(
            "media_control", "Громкость и управление музыкой/видео",
            lambda action, steps=1: self.media_control(action, steps),
            {"action": Param("string", "Действие", enum=list(MEDIA_KEYS)),
             "steps": Param("integer", "Сколько шагов громкости (1-10)", required=False)}))
        registry.register(Tool(
            "list_windows", "Перечислить открытые окна", lambda: self.list_windows()))
        registry.register(Tool(
            "window_action", "Переключиться на окно, свернуть, развернуть или восстановить его",
            lambda title, action: self.window_action(title, action),
            {"title": Param("string", "Часть заголовка окна"),
             "action": Param("string", "Действие", enum=["focus", "minimize", "maximize", "restore"])}))
        registry.register(Tool(
            "close_window", "Закрыть окно (несохранённые данные могут потеряться)",
            lambda title: self.window_action(title, "close"),
            {"title": Param("string", "Часть заголовка окна")},
            dangerous=True, confirm_template="закрыть окно «{title}»"))
