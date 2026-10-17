"""Обновление Джарвиса одной командой: update.bat (python -m jarvis.updater).

1. Правки владельца в config.toml переносятся в config.local.toml (он не в git), а сам
   config.toml возвращается к версии из репозитория — так git pull не даёт конфликтов.
   Копия прежнего файла остаётся в data/.
2. Другие изменённые файлы репозитория не трогаются: обновление останавливается и
   называет их — это может быть чужая работа (например, правки Claude Code).
3. git fetch + только fast-forward. Разошедшаяся история — честная ошибка, без слияний.
4. Если изменились requirements*.txt — доустановка пакетов (Claude и Silero — только если
   они уже стоят), после requirements.txt — Chromium для Playwright.
"""

from __future__ import annotations

import datetime as _dt
import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import LOCAL_CONFIG_NAME, PROJECT_ROOT, diff, dumps_toml, merge

LOCAL_HEADER = (
    "Личные настройки JARVIS: применяются поверх config.toml и не попадают в git.\n"
    "Сюда достаточно писать только изменённые значения, например:\n"
    "  [files]\n"
    "  allowed_dirs = [\"D:/Projects\"]"
)
SILERO_PIP = ["--index-url", "https://download.pytorch.org/whl/cpu",
              "--extra-index-url", "https://pypi.org/simple"]


@dataclass
class Result:
    code: int
    message: str


class UpdateError(Exception):
    pass


def _run_capture(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


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
                 now: Callable[[], _dt.datetime] = _dt.datetime.now):
        self.root = Path(root)
        self.python = python
        self.out = out
        self.run_live = run_live
        self.installed = installed
        self.now = now

    # --- git ---

    def git(self, *args: str, check: bool = True, strip: bool = True) -> str:
        proc = _run_capture(["git", "-c", "core.quotepath=off", *args], self.root)
        if check and proc.returncode != 0:
            raise UpdateError(f"git {' '.join(args)}: {(proc.stderr or proc.stdout).strip()}")
        return proc.stdout.strip() if strip else proc.stdout

    def modified_files(self) -> list[str]:
        # Строки вида «XY путь»: первая колонка может быть пробелом — не обрезать вывод.
        status = self.git("status", "--porcelain", "--untracked-files=no", strip=False)
        return [line[3:].strip().strip('"') for line in status.splitlines() if line.strip()]

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
        if shutil.which("git") is None:
            return Result(1, "Не найден git. Установите Git for Windows (git-scm.com) "
                             "и запустите update.bat снова.")
        if not (self.root / ".git").exists():
            return Result(1, "Папка установлена не через git clone — обновить автоматически "
                             "нельзя. Скачайте свежую версию или сделайте git clone.")
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

        if not self.git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False):
            return Result(1, "У текущей ветки нет ветки на сервере (upstream) — нечего обновлять.")
        self.out("Проверяю обновления...")
        self.git("fetch", "--quiet")
        behind = int(self.git("rev-list", "--count", "HEAD..@{u}") or 0)
        ahead = int(self.git("rev-list", "--count", "@{u}..HEAD") or 0)
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


def main(argv: list[str] | None = None) -> int:
    from . import console_utf8
    console_utf8()
    import argparse
    parser = argparse.ArgumentParser(prog="jarvis.updater", description="Обновить Джарвиса")
    parser.add_argument("--force", action="store_true",
                        help="не проверять, запущен ли Джарвис")
    argv = sys.argv[1:] if argv is None else argv
    # «/auto» из update.bat означает только «без паузы в конце».
    args = parser.parse_args([a for a in argv if a.lower() != "/auto"])
    if not args.force:
        pids = running_jarvis()
        if pids:
            print("Джарвис сейчас запущен (PID " + ", ".join(map(str, pids)) + "). "
                  "Закройте его и запустите обновление снова.")
            return 1
    result = Updater().update()
    print(result.message)
    return result.code


if __name__ == "__main__":
    sys.exit(main())
