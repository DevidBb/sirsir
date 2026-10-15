"""Безопасное управление Windows.

Всё, что можно сделать, явно перечислено: приложения берутся из config.toml
или из ярлыков меню «Пуск» (запускается сам ярлык, после проверки цели), сайты —
только http/https в браузере, поиск, открытие и удаление файлов ограничены
разрешёнными каталогами, программы и скрипты из папок пользователя не открываются,
команды не проходят через оболочку (никакого shell=True / PowerShell).
"""

from __future__ import annotations

import datetime as _dt
import os
import platform
import re
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any, Callable

from .app_finder import (DEFAULT_URL_SCHEMES, KNOWN_SITES, SEARCH_SITES, Shortcut, StartMenuIndex,
                         is_risky_file, parse_lnk, parse_url_file, search_url, site_url,
                         unsafe_target_reason, url_scheme_allowed)
from .config import Config
from .fuzzy import similarity
from .security import normalize
from .tool_registry import Choice, Param, Tool, ToolRegistry

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


def _default_power(action: str) -> None:
    """Блокировка, сон, выключение. Только Windows, без прав администратора."""
    if not IS_WINDOWS:
        raise OSError("управление питанием доступно только в Windows")
    import ctypes
    import threading
    if action == "lock":
        ctypes.windll.user32.LockWorkStation()  # type: ignore[attr-defined]
    elif action == "sleep":
        # Пауза, чтобы помощник успел договорить ответ.
        threading.Timer(3, lambda: ctypes.windll.powrprof.SetSuspendState(0, 1, 0)).start()  # type: ignore
    elif action in ("shutdown", "restart"):
        flag = "/s" if action == "shutdown" else "/r"
        subprocess.run(["shutdown", flag, "/t", str(SHUTDOWN_DELAY)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif action == "cancel":
        subprocess.run(["shutdown", "/a"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        raise ValueError(action)


SHUTDOWN_DELAY = 60
OPEN_KINDS = ["any", "app", "site", "folder", "file"]
# Порог похожести для файлов и папок пользователя и для известных сайтов.
PATH_THRESHOLD = 0.8
SITE_THRESHOLD = 0.85

Candidate = tuple[float, str, Callable[[], str]]


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
        try:
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
        except Exception as e:
            # Известная особенность pygetwindow: activate() бросает исключение с кодом 0
            # («операция успешно завершена»), хотя окно переключилось.
            if "Error code from Windows: 0" not in str(e):
                raise
        return win.title


class WindowsController:
    def __init__(self, config: Config,
                 launcher: Callable[[str], None] | None = None,
                 url_opener: Callable[[str], Any] | None = None,
                 window_backend: WindowBackend | None = None,
                 trash: Callable[[str], None] | None = None,
                 key_press: Callable[[int], None] | None = None,
                 power: Callable[[str], None] | None = None,
                 start_menu: StartMenuIndex | None = None):
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
        self.power = power or _default_power
        self.folders: dict[str, str] = config.get("windows.folders", {})
        dirs = config.get("windows.launcher.start_menu_dirs")
        self.start_menu = start_menu or StartMenuIndex(
            [Path(d).expanduser() for d in dirs] if dirs is not None else None,
            float(config.get("windows.launcher.cache_seconds", 600)))
        self.url_schemes = config.get("windows.launcher.url_schemes", DEFAULT_URL_SCHEMES)
        self.default_search = config.get("windows.web_search.engine", "google")

    # --- поиск по белым спискам ---
    # Частичное совпадение — только для осмысленных запросов: иначе «а» или пробел
    # от модели запустили бы первое попавшееся приложение («а» входит в «калькулятор»).
    MIN_PARTIAL_LEN = 3

    @classmethod
    def _lookup(cls, name: str, table: dict[str, str]) -> tuple[str, str] | None:
        q = normalize(name)
        if not q:
            return None
        for key, value in table.items():
            if normalize(key) == q:
                return key, value
        for key, value in table.items():
            k = normalize(key)
            if not k:
                continue
            if (len(k) >= cls.MIN_PARTIAL_LEN and k in q) or (len(q) >= cls.MIN_PARTIAL_LEN and q in k):
                return key, value
        return None

    @staticmethod
    def _launch_error(key: str, target: str, section: str, error: OSError) -> str:
        if isinstance(error, FileNotFoundError):
            return (f"Не удалось открыть {key}: «{target}» не найден на этом компьютере. "
                    f"Проверьте путь в config.toml, раздел [{section}].")
        return f"Не удалось открыть {key}: {error.strerror or error}."

    # --- приложения и сайты ---
    def open_app(self, name: str) -> str:
        found = self._lookup(name, self.apps)
        if found:
            key, command = found
            try:
                self.launch(command)
            except OSError as e:
                return self._launch_error(key, command, "windows.apps", e)
            return f"Запускаю {key}."
        result = self._pick(self._app_candidates(name))
        return result or (f"Программа «{name}» не в списке разрешённых и не найдена в меню «Пуск». "
                          f"Из списка доступны: {', '.join(self.apps)}.")

    def open_website(self, name: str) -> str:
        found = self._lookup(name, self.websites)
        if found:
            key, url = found
            if not url.startswith(("https://", "http://")):
                return f"Адрес сайта {key} в настройках некорректен."
            return self._open_url(key, url, "windows.websites")
        result = self._pick(self._site_candidates(name))
        return result or (f"Сайт «{name}» не в списке и не похож на адрес. "
                          "Назовите адрес, например «зайди на habr.com».")

    def _open_url(self, label: str, url: str, section: str = "windows.websites") -> str:
        # Только http/https и только в браузере: сам помощник страницу не загружает.
        if not url.startswith(("https://", "http://")):
            return f"Адрес {url} не открываю: разрешены только http и https."
        try:
            opened = self.open_url(url)
        except OSError as e:
            return self._launch_error(label, url, section, e)
        if opened is False:  # webbrowser.open: браузер не найден
            return "Не удалось открыть сайт: браузер по умолчанию не найден."
        return f"Открываю {label}."

    def _launch_shortcut(self, sc: Shortcut) -> str:
        if sc.path.suffix.lower() == ".url":
            try:
                url = parse_url_file(sc.path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                url = None
            if not url or not url_scheme_allowed(url, self.url_schemes):
                return f"Ярлык {sc.name} ведёт на неразрешённый адрес, не открываю."
        else:
            try:
                target, args = parse_lnk(sc.path.read_bytes()[:1 << 20])
            except OSError:
                target, args = None, ""
            reason = unsafe_target_reason(target, args)
            if reason:
                return f"Не запускаю {sc.name}: {reason}."
        try:
            self.launch(str(sc.path))  # сам ярлык через ShellExecute, без оболочки
        except OSError as e:
            return f"Не удалось запустить {sc.name}: {e.strerror or e}."
        return f"Запускаю {sc.name}."

    # --- поиск кандидатов для «открой …» ---
    def _app_candidates(self, name: str) -> list[Candidate]:
        out: list[Candidate] = []
        for key, command in self.apps.items():
            score = similarity(name, key)
            if score >= 0.99:
                out.append((1.01, key, lambda k=key: self.open_app(k)))
        for score, sc in self.start_menu.search(name):
            out.append((score, sc.name, lambda sc=sc: self._launch_shortcut(sc)))
        return out

    def _site_candidates(self, name: str) -> list[Candidate]:
        url = site_url(name)
        if url:
            host = re.sub(r"^https?://(?:www\.)?", "", url).rstrip("/")
            return [(1.01, host, lambda: self._open_url(host, url))]
        out: list[Candidate] = []
        for table in (self.websites, KNOWN_SITES):
            for key, address in table.items():
                score = similarity(name, key) + (0.005 if table is self.websites else 0)
                if score >= SITE_THRESHOLD:
                    out.append((min(score, 1.0), f"сайт {key}",
                                lambda k=key, a=address: self._open_url(k, a)))
        return out

    def _path_candidates(self, name: str, want: str) -> list[Candidate]:
        out: list[Candidate] = []
        if want in ("any", "folder"):
            for key, raw in self.folders.items():
                if similarity(name, key) >= 0.99 and Path(raw).expanduser().is_dir():
                    out.append((1.01, f"папка {key}",
                                lambda k=key, r=raw: self._open_path(Path(r).expanduser(), k)))
        for score, path in self.find_user_paths(name, want):
            label = ("папка " if path.is_dir() else "файл ") + path.name
            if path.parent.name:
                label += f" в папке {path.parent.name}"
            out.append((score, label, lambda p=path: self._open_path(p, p.name)))
        return out

    def find_user_paths(self, query: str, want: str = "any") -> list[tuple[float, Path]]:
        """Файлы и папки в разрешённых каталогах, похожие по имени на query."""
        if len(normalize(query)) < 2:
            return []
        found: list[tuple[float, Path]] = []
        scanned = 0
        for base in self.allowed_dirs:
            if not base.is_dir():
                continue
            for root, dirs, files in os.walk(base, followlinks=False):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                names = [(d, True) for d in dirs] if want in ("any", "folder") else []
                names += [(f, False) for f in files] if want in ("any", "file") else []
                for fname, is_dir in names:
                    scanned += 1
                    path = Path(root) / fname
                    if not is_dir and is_risky_file(path):
                        continue
                    stem = fname if is_dir else path.stem
                    score = similarity(query, re.sub(r"[_\-.]+", " ", stem))
                    if score >= PATH_THRESHOLD:
                        found.append((score, path))
                if scanned >= self.max_scanned:
                    break
        found.sort(key=lambda x: -x[0])
        return found[: self.max_results]

    def _open_path(self, path: Path, label: str) -> str:
        is_dir = path.is_dir()
        if not is_dir and not path.is_file():
            return f"{label} не найден на диске."
        if not is_dir and (not self._inside_allowed(path) or is_risky_file(path.resolve())):
            return f"Файл {label} не открываю: это программа, скрипт или он вне разрешённых папок."
        try:
            self.launch(str(path))
        except OSError as e:
            return f"Не удалось открыть {label}: {e.strerror or e}."
        return f"Открываю {'папку' if is_dir else 'файл'} {label}."

    @staticmethod
    def _pick(cands: list[Candidate], verb: str = "открыть") -> str | None:
        """Лучший вариант — выполнить; несколько равных — переспросить."""
        if not cands:
            return None
        best: dict[str, Candidate] = {}
        for c in sorted(cands, key=lambda c: -c[0]):
            best.setdefault(normalize(c[1]), c)
        ranked = list(best.values())
        top = ranked[0][0]
        margin = 0.0 if top >= 0.99 else 0.03
        close = [c for c in ranked if c[0] >= top - margin][:5]
        if len(close) == 1:
            return close[0][2]()
        listing = "; ".join(f"{i}) {c[1]}" for i, c in enumerate(close, 1))
        return Choice(f"Нашёл несколько вариантов: {listing}. Какой {verb}? "
                      "Скажите номер или название.", [(c[1], c[2]) for c in close])

    def open_anything(self, name: str, kind: str = "any") -> str:
        """«Открой …»: программа, сайт, папка или файл — что больше похоже."""
        if kind not in OPEN_KINDS:
            kind = "any"
        cands: list[Candidate] = []
        if kind in ("any", "site"):
            url_cands = self._site_candidates(name) if site_url(name) else []
            if url_cands:  # сказан адрес — это точно сайт
                return self._pick(url_cands) or ""
        if kind in ("any", "app"):
            cands += self._app_candidates(name)
        if kind in ("any", "site"):
            cands += self._site_candidates(name)
        if kind in ("folder", "file") or not cands or max(c[0] for c in cands) < 0.9:
            cands += self._path_candidates(name, kind if kind in ("folder", "file") else "any")
        result = self._pick(cands)
        if result is not None:
            return result
        what = {"app": "программу", "site": "сайт", "folder": "папку", "file": "файл"}.get(kind)
        if what:
            return f"Не нашёл {what} «{name}»."
        return f"Не нашёл «{name}» ни среди программ, ни среди сайтов, папок и файлов."

    # --- поиск на сайтах и в приложениях ---
    def web_search(self, query: str, site: str = "default") -> str:
        if site == "default" or site not in SEARCH_SITES:
            site = self.default_search if self.default_search in SEARCH_SITES else "google"
        title = SEARCH_SITES[site][0]
        url = search_url(site, query)
        if url.startswith("spotify:"):
            try:
                self.launch(url)  # ShellExecute откроет поиск в установленном Spotify
            except OSError:
                return "Spotify не установлен или не открылся."
            return f"Ищу «{query}» в Spotify."
        result = self._open_url(title, url)
        return f"Ищу «{query}»: {title}." if result.startswith("Открываю") else result

    # --- папки и файлы ---
    def open_folder(self, name: str) -> str:
        found = self._lookup(name, self.folders)
        if found:
            key, raw = found
            path = Path(raw).expanduser()
            if not path.is_dir():
                return f"Папка {key} не найдена на диске."
            try:
                self.launch(str(path))
            except OSError as e:
                return self._launch_error(key, str(path), "windows.folders", e)
            return f"Открываю папку {key}."
        result = self._pick(self._path_candidates(name, "folder"))
        return result or (f"Папка «{name}» не в списке и не найдена в папках пользователя. "
                          f"Из списка доступны: {', '.join(self.folders)}.")

    def open_file(self, name: str) -> str:
        result = self._pick(self._path_candidates(name, "file"))
        return result or f"Файл «{name}» не найден в разрешённых папках."

    # --- питание ---
    def power_action(self, action: str) -> str:
        messages = {
            "lock": "Блокирую компьютер.",
            "sleep": "Перевожу компьютер в сон.",
            "shutdown": f"Компьютер выключится через {SHUTDOWN_DELAY} секунд. "
                        "Чтобы отменить, скажите «отмени выключение».",
            "restart": f"Компьютер перезагрузится через {SHUTDOWN_DELAY} секунд. "
                       "Чтобы отменить, скажите «отмени выключение».",
            "cancel": "Выключение отменено.",
        }
        try:
            self.power(action)
        except (OSError, subprocess.CalledProcessError) as e:
            if action == "cancel":
                return "Запланированного выключения нет."
            return f"Не получилось: {e}."
        return messages[action]

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
            "open_app", "Запустить программу: из списка (" + ", ".join(self.apps)
            + ") или установленную (ищется по меню «Пуск»)",
            lambda name: self.open_app(name),
            {"name": Param("string", "Название программы")}))
        registry.register(Tool(
            "open_website", "Открыть сайт в браузере по названию или адресу (только http/https)",
            lambda name: self.open_website(name),
            {"name": Param("string", "Название сайта или адрес, например habr.com")}))
        registry.register(Tool(
            "web_search", "Найти что-то на сайте или в приложении (YouTube, Google, Яндекс, "
            "Википедия, ВК, Озон, Spotify и др.) — открывает страницу результатов поиска",
            lambda query, site="default": self.web_search(query, site),
            {"query": Param("string", "Что искать", max_length=200),
             "site": Param("string", "Где искать", required=False,
                           enum=["default", *SEARCH_SITES])}))
        registry.register(Tool(
            "open_file", "Открыть файл пользователя по имени (поиск в папках пользователя)",
            lambda name: self.open_file(name),
            {"name": Param("string", "Имя файла или его часть", max_length=100)}))
        registry.register(Tool(
            "open_anything", "Открыть что угодно по названию: программу, сайт, папку или файл",
            lambda name, kind="any": self.open_anything(name, kind),
            {"name": Param("string", "Название"),
             "kind": Param("string", "Что именно открыть", required=False, enum=OPEN_KINDS)}))
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
            "open_folder", "Открыть папку в проводнике: " + ", ".join(self.folders)
            + " или любую папку пользователя по имени",
            lambda name: self.open_folder(name),
            {"name": Param("string", "Название папки")}))
        registry.register(Tool(
            "lock_screen", "Заблокировать компьютер (экран блокировки)",
            lambda: self.power_action("lock")))
        registry.register(Tool(
            "sleep_computer", "Перевести компьютер в спящий режим",
            lambda: self.power_action("sleep"),
            dangerous=True, confirm_template="перевести компьютер в сон"))
        registry.register(Tool(
            "shutdown_computer", "Выключить компьютер (через минуту, можно отменить)",
            lambda: self.power_action("shutdown"),
            dangerous=True, confirm_template="выключить компьютер"))
        registry.register(Tool(
            "restart_computer", "Перезагрузить компьютер (через минуту, можно отменить)",
            lambda: self.power_action("restart"),
            dangerous=True, confirm_template="перезагрузить компьютер"))
        registry.register(Tool(
            "cancel_shutdown", "Отменить запланированное выключение или перезагрузку",
            lambda: self.power_action("cancel")))
        registry.register(Tool(
            "list_windows", "Перечислить открытые окна", lambda: self.list_windows()))
        # Отдельные инструменты вместо одного с параметром action: небольшие модели
        # часто забывают обязательный параметр («сверни окно» без action).
        for name, action, desc in (
                ("focus_window", "focus", "Переключиться на окно (сделать активным)"),
                ("minimize_window", "minimize", "Свернуть окно"),
                ("maximize_window", "maximize", "Развернуть окно на весь экран"),
                ("restore_window", "restore", "Восстановить свёрнутое окно")):
            registry.register(Tool(
                name, desc, (lambda a: lambda title: self.window_action(title, a))(action),
                {"title": Param("string", "Часть заголовка окна или название программы")}))
        registry.register(Tool(
            "close_window", "Закрыть окно (несохранённые данные могут потеряться)",
            lambda title: self.window_action(title, "close"),
            {"title": Param("string", "Часть заголовка окна")},
            dangerous=True, confirm_template="закрыть окно «{title}»"))
