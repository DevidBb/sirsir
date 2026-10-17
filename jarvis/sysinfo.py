"""Загрузка компьютера для верхней панели HUD: процессор, память, видеокарта.

Только настоящие значения: чего нет (psutil не установлен, нет nvidia-smi), то None —
HUD такое поле просто не показывает. nvidia-smi запускается не чаще раза в gpu_ttl секунд.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from typing import Any, Callable


def _nvidia_smi() -> str:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total",
         "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=4, creationflags=flags)
    return out.stdout if out.returncode == 0 else ""


def parse_gpu(text: str) -> dict[str, Any] | None:
    """Первая видеокарта из вывода nvidia-smi (csv без заголовка и единиц)."""
    line = text.strip().splitlines()[0] if text.strip() else ""
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 4:
        return None
    try:
        return {"name": parts[0], "util": float(parts[1]), "mem_used": float(parts[2]) / 1024,
                "mem_total": float(parts[3]) / 1024}
    except ValueError:
        return None


class SystemMonitor:
    def __init__(self, gpu_ttl: float = 5.0, run_smi: Callable[[], str] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.gpu_ttl = gpu_ttl
        self.clock = clock
        self._run_smi = run_smi or (_nvidia_smi if shutil.which("nvidia-smi") else None)
        self._gpu: dict[str, Any] | None = None
        self._gpu_at = float("-inf")
        self._lock = threading.Lock()

    def gpu(self) -> dict[str, Any] | None:
        if self._run_smi is None:
            return None
        with self._lock:
            if self.clock() - self._gpu_at >= self.gpu_ttl:
                self._gpu_at = self.clock()
                try:
                    self._gpu = parse_gpu(self._run_smi())
                except (OSError, subprocess.SubprocessError):
                    self._gpu = None
            return self._gpu

    def snapshot(self) -> dict[str, Any]:
        data: dict[str, Any] = {"cpu": None, "ram_used": None, "ram_total": None, "gpu": self.gpu()}
        try:
            import psutil
        except ImportError:
            return data
        data["cpu"] = round(psutil.cpu_percent(interval=None), 1)  # с прошлого вызова
        vm = psutil.virtual_memory()
        data["ram_used"] = round((vm.total - vm.available) / 2**30, 1)
        data["ram_total"] = round(vm.total / 2**30, 1)
        return data
