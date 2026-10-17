"""Запуск HUD с перезапуском после обновления (run_web.bat → python -m jarvis.supervisor ...).

Аргументы передаются ``python -m jarvis`` как есть. Если Джарвис завершился с кодом
``RESTART_EXIT_CODE`` (кнопка «Обновить и перезапустить» в HUD), супервизор запускает
обновление (``python -m jarvis.updater --from-hud``) уже после выхода Джарвиса — pip может
заменить пакеты, которые тот держал открытыми, — и запускает Джарвиса снова, без новой
вкладки браузера. Любой другой код выхода — обычное завершение.

Сам супервизор импортирует только стандартную библиотеку: обновление может заменить любой
файл проекта, а этот процесс продолжает работать со старым кодом в памяти.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
RESTART_EXIT_CODE = 75   # «обновить и перезапустить» (кнопка в HUD)
SUPERVISOR_ENV = "JARVIS_SUPERVISED"
DRY_RUN_ENV = "JARVIS_UPDATE_DRY_RUN"  # проверка в CI: перезапуск без изменения файлов
MAX_RESTARTS = 20


def run_child(cmd: list[str]) -> int:
    env = {**os.environ, SUPERVISOR_ENV: "1"}
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env)
    interrupts = 0
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            # Ctrl+C получает и Джарвис — он завершится штатно (закроет браузер, Claude, микрофон).
            interrupts += 1
            if interrupts >= 3:
                proc.terminate()


def supervise(args: list[str], python: str = sys.executable,
              run: Callable[[list[str]], int] = run_child,
              out: Callable[[str], None] = print) -> int:
    args = list(args)
    for _ in range(MAX_RESTARTS + 1):
        code = run([python, "-m", "jarvis", *args])
        if code != RESTART_EXIT_CODE:
            return code
        out("\n=== Обновление JARVIS ===")
        update = [python, "-m", "jarvis.updater", "--from-hud"]
        if os.environ.get(DRY_RUN_ENV) == "1":
            update.append("--dry-run")
        run(update)
        out("=== Перезапуск JARVIS ===\n")
        if "--no-open" not in args:
            args.append("--no-open")  # вкладка HUD уже открыта и переподключится сама
    out("Слишком много перезапусков подряд — остановлено.")
    return 1


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    return supervise(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main())
