"""Диагностический отчёт: всё, что нужно, чтобы разобраться в проблеме удалённо.

    python -m jarvis --report     (или report.bat)

Собирает версии, видеокарту, результаты проверок, ключевые настройки и конец
лога в data/report.txt. Звук, заметки, память и история разговоров в отчёт
не попадают.
"""

from __future__ import annotations

import datetime as _dt
import importlib.metadata
import io
import platform
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

from . import __version__, checks
from .config import Config

PACKAGES = ["numpy", "sounddevice", "vosk", "faster-whisper", "ctranslate2", "onnxruntime",
            "pyttsx3", "torch", "psutil", "Send2Trash", "pystray", "Pillow", "PyGetWindow",
            "nvidia-cublas-cu12", "nvidia-cudnn-cu12"]
CONFIG_KEYS = ["llm.model", "llm.num_ctx", "llm.num_predict", "speech_recognition.model",
               "speech_recognition.device", "tts.engine", "tts.silero_speaker", "audio.input_device",
               "wake_word.phrases", "assistant.barge_in", "assistant.wake_feedback"]


def _version(pkg: str) -> str:
    try:
        return importlib.metadata.version(pkg)
    except importlib.metadata.PackageNotFoundError:
        return "—"


def _gpu() -> str:
    if shutil.which("nvidia-smi") is None:
        return "nvidia-smi не найден"
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total,memory.used",
             "--format=csv,noheader"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or out.stderr.strip()
    except Exception as e:
        return f"ошибка: {e}"


def _audio_devices() -> str:
    try:
        import sounddevice as sd  # type: ignore
        default_in = sd.default.device[0]
        lines = []
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                mark = "*" if i == default_in else " "
                lines.append(f" {mark}{i}: {d['name']} ({int(d['default_samplerate'])} Гц)")
        return "\n".join(lines[:15]) or "нет устройств ввода"
    except Exception as e:
        return f"недоступно: {e}"


def _log_tail(path: Path, lines: int = 150) -> str:
    if not path.is_file():
        return "(лога нет)"
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(text[-lines:])


def build(config: Config) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        checks.report(checks.run_all(config, voice=True))
    parts = [
        f"JARVIS {__version__}, отчёт от {_dt.datetime.now():%Y-%m-%d %H:%M}",
        f"Система: {platform.platform()}; Python {sys.version.split()[0]} ({platform.architecture()[0]})",
        f"Процессор: {platform.processor() or platform.machine()}",
        f"Видеокарта: {_gpu()}",
        "",
        "== Пакеты ==",
        *[f"  {p}: {_version(p)}" for p in PACKAGES],
        "",
        "== Микрофоны (* — по умолчанию) ==",
        _audio_devices(),
        "",
        "== Настройки ==",
        *[f"  {k} = {config.get(k)!r}" for k in CONFIG_KEYS],
        "",
        "== Проверки ==",
        buf.getvalue().rstrip(),
        "",
        "== Конец лога ==",
        _log_tail(config.path("assistant.log_file")),
    ]
    return "\n".join(parts) + "\n"


def write(config: Config) -> Path:
    path = config.path("memory.db_path").parent / "report.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build(config), encoding="utf-8")
    return path
