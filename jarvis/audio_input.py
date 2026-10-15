"""Захват звука с микрофона и определение конца речи.

Звук только в памяти (очередь numpy-блоков), на диск ничего не пишется.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Iterator

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class VadConfig:
    sample_rate: int = 16000
    block_ms: int = 30
    silence_ms_to_stop: int = 900
    max_command_seconds: float = 15
    no_speech_timeout_seconds: float = 6
    energy_ratio: float = 3.0
    min_speech_ms: int = 250


def rms(block: np.ndarray) -> float:
    if block.size == 0:
        return 0.0
    x = block.astype(np.float32)
    if block.dtype == np.int16:
        x /= 32768.0
    return float(np.sqrt(np.mean(x * x)))


class EndpointDetector:
    """Энергетический VAD с адаптивной оценкой фонового шума.

    Речь — блок громче шума в energy_ratio раз. Конец фразы — тишина
    дольше silence_ms_to_stop после хотя бы min_speech_ms речи.
    """

    MIN_THRESHOLD = 0.006

    def __init__(self, cfg: VadConfig, noise_floor: float = 0.003):
        self.cfg = cfg
        self.noise = noise_floor
        self.speech_ms = 0
        self.silence_ms = 0
        self.total_ms = 0
        self.started = False

    def threshold(self) -> float:
        return max(self.noise * self.cfg.energy_ratio, self.MIN_THRESHOLD)

    def calibrate(self, block: np.ndarray) -> None:
        self.noise = 0.9 * self.noise + 0.1 * rms(block)

    def feed(self, block: np.ndarray) -> str:
        """Возвращает "continue", "end" (фраза закончена) или "timeout"."""
        ms = int(1000 * len(block) / self.cfg.sample_rate)
        self.total_ms += ms
        level = rms(block)
        if level >= self.threshold():
            self.speech_ms += ms
            self.silence_ms = 0
            if self.speech_ms >= self.cfg.min_speech_ms:
                self.started = True
        else:
            self.silence_ms += ms
            if not self.started:
                # Пока речь не началась — подстраиваемся под шум.
                self.noise = 0.95 * self.noise + 0.05 * level
                self.speech_ms = max(0, self.speech_ms - ms)
        if self.started and self.silence_ms >= self.cfg.silence_ms_to_stop:
            return "end"
        if self.total_ms >= self.cfg.max_command_seconds * 1000:
            return "end" if self.started else "timeout"
        if not self.started and self.total_ms >= self.cfg.no_speech_timeout_seconds * 1000:
            return "timeout"
        return "continue"


def resample(block: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Пересчёт частоты для речи. Кратные частоты (48→16 кГц) — усреднением
    (заодно гасит частоты выше новой границы), прочие — линейной интерполяцией."""
    if src_rate == dst_rate or block.size == 0:
        return block
    dtype = block.dtype
    x = block.astype(np.float32)
    if src_rate % dst_rate == 0:
        k = src_rate // dst_rate
        n = len(x) // k
        y = x[: n * k].reshape(n, k).mean(axis=1)
    else:
        n = int(round(len(x) * dst_rate / src_rate))
        y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return y.astype(dtype)


class Microphone:
    """Поток int16 моно-блоков с микрофона через sounddevice."""

    def __init__(self, sample_rate: int = 16000, block_ms: int = 30, device: str | int | None = None):
        self.sample_rate = sample_rate
        self.block_size = int(sample_rate * block_ms / 1000)
        self.device = self._parse_device(device)
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=400)
        self._stream = None
        self._muted = threading.Event()
        self.error: str | None = None
        self.native_rate: int | None = None  # если устройство не умеет sample_rate

    @staticmethod
    def _parse_device(device):
        if device in (None, ""):
            return None
        if isinstance(device, str) and device.isdigit():
            return int(device)
        return device

    def _callback(self, indata, frames, time_info, status) -> None:
        if self._muted.is_set():
            return
        block = indata[:, 0].copy()
        if self.native_rate:
            block = resample(block, self.native_rate, self.sample_rate)
        try:
            self._queue.put_nowait(block)
        except queue.Full:
            pass  # потребитель не успевает — старые данные не нужны

    def start(self) -> None:
        import sounddevice as sd  # импорт здесь: модуль нужен только с микрофоном
        try:
            self._stream = sd.InputStream(samplerate=self.sample_rate, channels=1, dtype="int16",
                                          blocksize=self.block_size, device=self.device,
                                          callback=self._callback)
        except Exception as e:
            # Некоторые устройства (WASAPI) не умеют 16 кГц — пишем на родной
            # частоте устройства и пересчитываем сами.
            info = sd.query_devices(self.device, "input")
            native = int(info["default_samplerate"])
            if native == self.sample_rate:
                raise
            log.warning("Микрофон не поддерживает %d Гц (%s), записываю на %d Гц с пересчётом",
                        self.sample_rate, e, native)
            self.native_rate = native
            self._stream = sd.InputStream(samplerate=native, channels=1, dtype="int16",
                                          blocksize=int(native * self.block_size / self.sample_rate),
                                          device=self.device, callback=self._callback)
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    @property
    def muted(self) -> bool:
        return self._muted.is_set()

    def mute(self) -> None:
        self._muted.set()
        self.flush()

    def unmute(self) -> None:
        self.flush()
        self._muted.clear()

    def flush(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def blocks(self, stop_event: threading.Event | None = None,
               timeout: float = 0.5) -> Iterator[np.ndarray]:
        """Бесконечный генератор блоков; при отключении устройства — выход."""
        while not (stop_event and stop_event.is_set()):
            try:
                yield self._queue.get(timeout=timeout)
            except queue.Empty:
                if self._stream is not None and not self._stream.active:
                    self.error = "Микрофон отключён или недоступен"
                    return
                continue


def record_command(blocks: Iterator[np.ndarray], cfg: VadConfig,
                   noise_floor: float = 0.003) -> np.ndarray | None:
    """Записывает фразу до паузы. Возвращает float32 [-1, 1] или None (тишина)."""
    detector = EndpointDetector(cfg, noise_floor)
    chunks: list[np.ndarray] = []
    preroll: list[np.ndarray] = []
    for block in blocks:
        state = detector.feed(block)
        if detector.started:
            if preroll:
                chunks.extend(preroll)
                preroll = []
            chunks.append(block)
        else:
            preroll = (preroll + [block])[-10:]  # ~300 мс до начала речи
        if state != "continue":
            break
    if not detector.started or not chunks:
        return None
    audio = np.concatenate(chunks)
    if audio.dtype == np.int16:
        return audio.astype(np.float32) / 32768.0
    return audio.astype(np.float32)


def wait_seconds(blocks: Iterator[np.ndarray], seconds: float) -> None:
    deadline = time.monotonic() + seconds
    for _ in blocks:
        if time.monotonic() >= deadline:
            return
