"""Озвучивание ответов локальным TTS (SAPI5 через pyttsx3) с возможностью прервать."""

from __future__ import annotations

import logging
import re
import threading

log = logging.getLogger(__name__)


def split_sentences(text: str, max_len: int = 250) -> list[str]:
    """Режем ответ на предложения, чтобы речь можно было прервать между ними."""
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    out: list[str] = []
    for p in parts:
        while len(p) > max_len:
            cut = p.rfind(" ", 0, max_len)
            cut = cut if cut > 0 else max_len
            out.append(p[:cut])
            p = p[cut:].strip()
        if p:
            out.append(p)
    return out


def beep_tone(sample_rate: int = 22050, freq: float = 880, ms: int = 120):
    import numpy as np
    t = np.arange(int(sample_rate * ms / 1000)) / sample_rate
    fade = np.minimum(1, np.minimum(t, t[-1] - t) * 200)  # без щелчков на краях
    return (0.25 * np.sin(2 * np.pi * freq * t) * fade).astype(np.float32)


def play_beep() -> None:
    """Короткий сигнал «слушаю» — быстрее, чем произносить слово."""
    try:
        import sounddevice as sd  # type: ignore
        sd.play(beep_tone(), 22050)
        sd.wait()
    except Exception as e:
        log.debug("Сигнал не воспроизведён: %s", e)


class Speaker:
    def __init__(self, engine: str = "pyttsx3", rate: int = 185, volume: float = 1.0,
                 voice_hint: str = "ru"):
        self.engine_name = engine
        self.rate = rate
        self.volume = volume
        self.voice_hint = voice_hint.lower()
        self._engine = None
        self._lock = threading.Lock()
        self._interrupt = threading.Event()
        self.voice_name: str | None = None

    def load(self) -> None:
        if self.engine_name == "print":
            return
        if self.engine_name != "pyttsx3":
            raise ValueError(f"Неизвестный TTS-движок: {self.engine_name}")
        import pyttsx3  # type: ignore
        self._engine = pyttsx3.init()
        self._select_voice()
        self.apply_settings()

    def _select_voice(self) -> None:
        voices = self._engine.getProperty("voices") or []
        for v in voices:
            meta = f"{v.id} {v.name} {getattr(v, 'languages', '')}".lower()
            if self.voice_hint in meta or "russian" in meta or "irina" in meta or "pavel" in meta:
                self._engine.setProperty("voice", v.id)
                self.voice_name = v.name
                return
        log.warning("Русский голос не найден; установите его в Параметрах Windows → Время и язык → Речь")

    def apply_settings(self) -> None:
        if self._engine is not None:
            self._engine.setProperty("rate", int(self.rate))
            self._engine.setProperty("volume", float(self.volume))

    def interrupt(self) -> None:
        self._interrupt.set()
        if self._engine is not None:
            try:
                self._engine.stop()
            except Exception:
                pass

    def say(self, text: str) -> None:
        if not text:
            return
        self._interrupt.clear()
        print(f"JARVIS: {text}", flush=True)
        if self._engine is None:
            return
        with self._lock:
            for sentence in split_sentences(text):
                if self._interrupt.is_set():
                    break
                self._engine.say(sentence)
                self._engine.runAndWait()
