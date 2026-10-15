"""Автозапуск при входе в Windows через папку «Автозагрузка» текущего пользователя.

Права администратора и реестр не нужны: в папку кладётся маленький
JARVIS.cmd, который запускает run.bat свёрнутым и со значком в трее.
Включается и выключается только вручную: python -m jarvis --autostart on|off.
"""

from __future__ import annotations

import os
from pathlib import Path

from .config import PROJECT_ROOT

FILE_NAME = "JARVIS.cmd"


def startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise OSError("Переменная APPDATA не задана — автозапуск поддерживается только в Windows")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def script_text(project_root: Path) -> str:
    # chcp 65001 — чтобы путь с кириллицей прочитался правильно.
    return ("@echo off\r\n"
            "chcp 65001 >nul\r\n"
            f'cd /d "{project_root}"\r\n'
            'start "JARVIS" /min cmd /c run.bat --tray\r\n')


def enable(project_root: Path = PROJECT_ROOT, folder: Path | None = None) -> Path:
    folder = folder or startup_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / FILE_NAME
    # write_bytes: на Windows write_text превратил бы \r\n в \r\r\n.
    path.write_bytes(script_text(project_root.resolve()).encode("utf-8"))
    return path


def disable(folder: Path | None = None) -> bool:
    path = (folder or startup_dir()) / FILE_NAME
    if path.exists():
        path.unlink()
        return True
    return False


def is_enabled(folder: Path | None = None) -> bool:
    try:
        return ((folder or startup_dir()) / FILE_NAME).exists()
    except OSError:
        return False
