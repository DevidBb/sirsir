"""Обновление Джарвиса: update.bat (python -m jarvis.updater) или кнопка в HUD.

1. Правки владельца в config.toml переносятся в config.local.toml (он не в git), а сам
   config.toml возвращается к версии из репозитория — так git pull не даёт конфликтов.
   Копия прежнего файла остаётся в data/.
2. Другие изменённые файлы репозитория не трогаются: обновление останавливается и
   называет их — это может быть чужая работа (например, правки Claude Code).
3. git fetch + только fast-forward. Разошедшаяся история — честная ошибка, без слияний.
4. Если изменились requirements*.txt — доустановка пакетов (Claude и Silero — только если
   они уже стоят), после requirements.txt — Chromium для Playwright.

Для HUD: ``Updater.check()`` только смотрит (git fetch, ничего не меняет), ``UpdateChecker``
делает это в фоне. Кнопка «Обновить и перезапустить» завершает Джарвиса с кодом
``RESTART_EXIT_CODE``; jarvis.supervisor запускает ``--from-hud`` (результат — в
data/last_update.json, его покажет HUD) и снова запускает Джарвиса.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import LOCAL_CONFIG_NAME, PROJECT_ROOT, diff, dumps_toml, merge

LOCAL_HEADER = (
    "Личные настройки JARVIS: применяются поверх config.toml и не попадают в git.\n"
    "Сюда достаточно писать только изменённые значения, например:\n"
    "  [files]\n"
    "  allowed_dirs = [\"D:/Projects\"]"
)
SILERO_PIP = ["--index-url", "https://download.pytorch.org/whl/cpu",
              "--extra-index-url", "https://pypi.org/simple"]
RESULT_NAME = "last_update.json"
REQUIREMENTS = re.compile(r"^requirements(-\w+)?\.txt$")
GIT_TIMEOUT = 30
FETCH_TIMEOUT = 90
# Фоновая проверка не должна открывать окно входа GitHub или ждать пароль в консоли.
QUIET_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}


@dataclass
class Result:
    code: int
    message: str


class UpdateError(Exception):
    pass


def _run_capture(cmd: list[str], cwd: Path, env: dict[str, str] | None = None,
                 timeout: float | None = None) -> subprocess.CompletedProcess:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # без мигающих окон консоли
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, timeout=timeout, creationflags=flags)


def _run_live(cmd: list[str], cwd: Path) -> int:
    return subprocess.run(cmd, cwd=cwd).returncode


def _installed(module: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def running_jarvis() -> list[int]:
    """PID других запущенных процессов Джарвиса (pip не обновит занятые ими DLL)."""
    try:
        import psutil
    except ImportError:
        return []
    found = []
    me = os.getpid()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = proc.info["cmdline"] or []
        except Exception:
            continue
        if proc.info["pid"] == me or len(cmd) < 3:
            continue
        if "python" in Path(cmd[0]).name.lower() and "-m" in cmd and "jarvis" in cmd:
            found.append(proc.info["pid"])
    return found


class Updater:
    def __init__(self, root: Path = PROJECT_ROOT, python: str = sys.executable,
                 out: Callable[[str], None] = print,
                 run_live: Callable[[list[str], Path], int] = _run_live,
                 installed: Callable[[str], bool] = _installed,
                 now: Callable[[], _dt.datetime] = _dt.datetime.now,
                 interactive: bool = True):
        self.root = Path(root)
        self.python = python
        self.out = out
        self.run_live = run_live
        self.installed = installed
        self.now = now
        self.interactive = interactive  # False — без окон входа и запросов пароля

    # --- git ---

    def git(self, *args: str, check: bool = True, strip: bool = True,
            timeout: float = GIT_TIMEOUT) -> str:
        env = None if self.interactive else {**os.environ, **QUIET_GIT_ENV}
        try:
            proc = _run_capture(["git", "-c", "core.quotepath=off", *args], self.root, env, timeout)
        except subprocess.TimeoutExpired:
            raise UpdateError(f"git {args[0]}: нет ответа за {int(timeout)} с (сеть или GitHub недоступны)")
        if check and proc.returncode != 0:
            raise UpdateError(f"git {' '.join(args)}: {(proc.stderr or proc.stdout).strip()}")
        return proc.stdout.strip() if strip else proc.stdout

    def unsupported(self) -> str | None:
        """Почему обновить нельзя в принципе (нет git, установка не из git clone)."""
        if shutil.which("git") is None:
            return ("Не найден git. Установите Git for Windows (git-scm.com) "
                    "и запустите update.bat снова.")
        if not (self.root / ".git").exists():
            return ("Папка установлена не через git clone — обновить автоматически "
                    "нельзя. Скачайте свежую версию или сделайте git clone.")
        return None

    def modified_files(self) -> list[str]:
        # Строки вида «XY путь»: первая колонка может быть пробелом — не обрезать вывод.
        status = self.git("status", "--porcelain", "--untracked-files=no", strip=False)
        return [line[3:].strip().strip('"') for line in status.splitlines() if line.strip()]

    def has_upstream(self) -> bool:
        return bool(self.git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False))

    def fetch(self) -> None:
        self.git("fetch", "--quiet", timeout=FETCH_TIMEOUT)

    def counts(self) -> tuple[int, int]:
        """(новых коммитов на сервере, своих неотправленных)."""
        behind = int(self.git("rev-list", "--count", "HEAD..@{u}") or 0)
        ahead = int(self.git("rev-list", "--count", "@{u}..HEAD") or 0)
        return behind, ahead

    def commits(self, spec: str, limit: int = 30) -> list[dict[str, Any]]:
        out = self.git("log", "--format=%h%x1f%ct%x1f%s", f"--max-count={limit}", spec)
        items = []
        for line in out.splitlines():
            sha, ts, subject = (line.split("\x1f") + ["", ""])[:3]
            items.append({"sha": sha, "time": int(ts or 0), "subject": subject})
        return items

    def version(self) -> dict[str, Any] | None:
        """Текущий коммит: {sha, time, subject} или None, если это не git-установка."""
        if self.unsupported():
            return None
        try:
            items = self.commits("HEAD", limit=1)
        except UpdateError:
            return None
        return items[0] if items else None

    # --- только посмотреть (для HUD) ---

    def check(self) -> dict[str, Any]:
        """Есть ли обновление. Только git fetch — файлы и ветка не меняются."""
        status: dict[str, Any] = {
            "checked_at": time.time(), "available": False, "behind": 0, "ahead": 0,
            "commits": [], "blocked": [], "config_edited": False, "needs_packages": False,
            "can_apply": False, "current": None, "error": self.unsupported(),
        }
        if status["error"]:
            return status
        try:
            status["current"] = self.version()
            modified = self.modified_files()
            status["config_edited"] = "config.toml" in modified
            status["blocked"] = [f for f in modified if f != "config.toml"]
            if not self.has_upstream():
                status["error"] = "у текущей ветки нет ветки на сервере (upstream) — обновлять неоткуда"
                return status
            self.fetch()
            behind, ahead = self.counts()
            status.update(behind=behind, ahead=ahead, available=behind > 0)
            if behind:
                status["commits"] = self.commits("HEAD..@{u}")
                changed = self.git("diff", "--name-only", "HEAD", "@{u}").splitlines()
                status["needs_packages"] = any(REQUIREMENTS.match(name) for name in changed)
            status["can_apply"] = behind > 0 and not ahead and not status["blocked"]
        except UpdateError as e:
            status["error"] = str(e)
        return status

    # --- config.toml → config.local.toml ---

    def move_config_edits(self) -> str:
        """Перенести правки config.toml в config.local.toml и вернуть config.toml из git."""
        path = self.root / "config.toml"
        try:
            current = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise UpdateError(f"в config.toml ошибка ({e}) — исправьте её или верните файл: "
                              "git checkout -- config.toml")
        base = tomllib.loads(self.git("show", "HEAD:config.toml"))
        changed, removed = diff(base, current)

        local_path = self.root / LOCAL_CONFIG_NAME
        local: dict = {}
        if local_path.is_file():
            try:
                local = tomllib.loads(local_path.read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError as e:
                raise UpdateError(f"в {LOCAL_CONFIG_NAME} ошибка: {e}")
        merge(local, changed)  # свежие правки в config.toml важнее

        backup_dir = self.root / "data"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"config.toml.{self.now():%Y%m%d-%H%M%S}.bak"
        shutil.copy2(path, backup)
        if changed:
            local_path.write_text(dumps_toml(local, LOCAL_HEADER), encoding="utf-8")
        self.git("checkout", "--", "config.toml")

        lines = [f"Правки config.toml перенесены в {LOCAL_CONFIG_NAME}"
                 if changed else "В config.toml не было изменений значений",
                 f"копия прежнего файла: {backup.relative_to(self.root)}"]
        if removed:
            lines.append("удалённые из config.toml ключи вернутся к значениям по умолчанию: "
                         + ", ".join(removed))
        return "; ".join(lines)

    # --- зависимости ---

    def install_requirements(self, changed: set[str]) -> list[str]:
        """Доустановить пакеты по изменившимся requirements*.txt. Возвращает ошибки."""
        errors = []
        pip = [self.python, "-m", "pip", "install"]
        steps: list[tuple[str, list[str]]] = []
        if "requirements.txt" in changed:
            steps.append(("основные пакеты", pip + ["-r", "requirements.txt"]))
            steps.append(("Chromium для браузера",
                          [self.python, "-m", "playwright", "install", "chromium"]))
        if "requirements-claude.txt" in changed and self.installed("claude_agent_sdk"):
            steps.append(("Claude Agent SDK", pip + ["-r", "requirements-claude.txt"]))
        if "requirements-silero.txt" in changed and self.installed("torch"):
            steps.append(("голос Silero", pip + ["-r", "requirements-silero.txt"] + SILERO_PIP))
        if "requirements-dev.txt" in changed and self.installed("pytest"):
            steps.append(("пакеты для тестов", pip + ["-r", "requirements-dev.txt"]))
        for title, cmd in steps:
            self.out(f"Обновляю: {title}...")
            if self.run_live(cmd, self.root) != 0:
                errors.append(f"не удалось обновить {title}: {' '.join(cmd[1:])}")
        return errors

    # --- всё вместе ---

    def update(self) -> Result:
        problem = self.unsupported()
        if problem:
            return Result(1, problem)
        try:
            return self._update()
        except UpdateError as e:
            return Result(1, f"Обновление остановлено: {e}")

    def _update(self) -> Result:
        modified = self.modified_files()
        if "config.toml" in modified:
            self.out(self.move_config_edits())
            modified.remove("config.toml")
        if modified:
            return Result(1, "Изменены файлы репозитория — обновление их не трогает, чтобы не "
                             "потерять работу:\n  " + "\n  ".join(modified) +
                             "\nЗакоммитьте их (git commit) или верните (git checkout -- <файл>) "
                             "и запустите update.bat снова.")

        if not self.has_upstream():
            return Result(1, "У текущей ветки нет ветки на сервере (upstream) — нечего обновлять.")
        self.out("Проверяю обновления...")
        self.fetch()
        behind, ahead = self.counts()
        if behind == 0:
            extra = f" (локальных коммитов, ещё не отправленных: {ahead})" if ahead else ""
            return Result(0, "Уже последняя версия" + extra + ".")
        if ahead:
            return Result(1, f"История разошлась: на сервере {behind} новых коммитов, здесь {ahead} "
                             "своих. Автоматически это не объединить — сделайте git pull --rebase "
                             "вручную или попросите Claude Code.")

        old = self.git("rev-parse", "HEAD")
        self.git("merge", "--ff-only", "--quiet", "@{u}")
        new = self.git("rev-parse", "HEAD")
        changed = set(self.git("diff", "--name-only", old, new).splitlines())
        log = self.git("log", "--format=  %h %s", "--max-count=20", f"{old}..{new}")
        self.out(f"Получено коммитов: {behind}\n{log}")

        errors = self.install_requirements(changed)
        if errors:
            return Result(1, "Код обновлён, но пакеты — не все:\n  " + "\n  ".join(errors) +
                             "\nПовторите update.bat или запустите install.bat.")
        return Result(0, "Готово: Джарвис обновлён. Перезапустите его.")


# --- результат для HUD после перезапуска ---

def result_path(root: Path = PROJECT_ROOT) -> Path:
    return Path(root) / "data" / RESULT_NAME


def write_result(data: dict[str, Any], root: Path = PROJECT_ROOT) -> Path:
    path = result_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def take_result(root: Path = PROJECT_ROOT, max_age: float = 3600) -> dict[str, Any] | None:
    """Прочитать и удалить итог обновления из HUD (старые итоги не показываются)."""
    path = result_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    try:
        fresh = isinstance(data, dict) and time.time() - float(data.get("at", 0)) <= max_age
    except (TypeError, ValueError):
        fresh = False
    return data if fresh else None


def run_from_hud(updater: Updater, dry_run: bool = False) -> int:
    """Обновление по кнопке HUD (Джарвис уже закрыт): итог — в data/last_update.json."""
    before = updater.version()
    if dry_run:
        st = updater.check()
        result = Result(0 if not st["error"] else 1,
                        "Проверка без изменений: " + (st["error"] or f"новых коммитов {st['behind']}"))
    else:
        result = updater.update()
    after = updater.version()
    commits: list[dict[str, Any]] = []
    if before and after and before["sha"] != after["sha"]:
        try:
            commits = updater.commits(f"{before['sha']}..{after['sha']}", limit=20)
        except UpdateError:
            pass
    updater.out(result.message)
    write_result({"at": time.time(), "ok": result.code == 0, "message": result.message,
                  "from": before, "to": after, "commits": commits, "dry_run": dry_run}, updater.root)
    return result.code


class UpdateChecker:
    """Фоновая проверка обновлений для HUD: при запуске и раз в interval часов."""

    def __init__(self, updater: Updater, publish: Callable[[dict], None],
                 interval_hours: float = 6.0, first_delay: float = 20.0):
        self.updater = updater
        self.publish = publish
        self.interval = max(0.25, float(interval_hours)) * 3600
        self.first_delay = first_delay
        self.status: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def check_now(self) -> dict[str, Any]:
        with self._lock:  # кнопка «Проверить» и таймер не запускают git одновременно
            status = self.updater.check()
            self.status = status
        self.publish({"type": "update", **status})
        return status

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="update-check", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        delay = self.first_delay
        while not self._stop.wait(delay):
            try:
                self.check_now()
            except Exception:  # noqa: BLE001 — проверка обновлений не должна ронять HUD
                import logging
                logging.getLogger(__name__).exception("Проверка обновлений")
            delay = self.interval


def main(argv: list[str] | None = None) -> int:
    from . import console_utf8
    console_utf8()
    import argparse
    parser = argparse.ArgumentParser(prog="jarvis.updater", description="Обновить Джарвиса")
    parser.add_argument("--force", action="store_true",
                        help="не проверять, запущен ли Джарвис")
    parser.add_argument("--from-hud", action="store_true",
                        help="запуск из jarvis.supervisor после кнопки в HUD: итог — в data/last_update.json")
    parser.add_argument("--dry-run", action="store_true", help="только проверить, ничего не менять")
    argv = sys.argv[1:] if argv is None else argv
    # «/auto» из update.bat означает только «без паузы в конце».
    args = parser.parse_args([a for a in argv if a.lower() != "/auto"])
    if args.from_hud:
        return run_from_hud(Updater(), dry_run=args.dry_run)
    if not args.force:
        pids = running_jarvis()
        if pids:
            print("Джарвис сейчас запущен (PID " + ", ".join(map(str, pids)) + "). "
                  "Закройте его и запустите обновление снова.")
            return 1
    if args.dry_run:
        st = Updater().check()
        print(st["error"] or f"Новых коммитов: {st['behind']}, своих неотправленных: {st['ahead']}.")
        return 1 if st["error"] else 0
    result = Updater().update()
    print(result.message)
    return result.code


if __name__ == "__main__":
    sys.exit(main())
