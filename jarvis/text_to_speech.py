"""Озвучивание ответов локальным TTS с возможностью прервать.

Движки:
- "silero"  — нейросетевой русский голос Silero v5 (или v4, если скачан только он;
              torch, CPU);
- "pyttsx3" — системные голоса Windows (SAPI5), ничего не скачивает;
- "print"   — только вывод в консоль (текстовый режим и тесты).
Если Silero недоступен, используется pyttsx3.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# Голоса Silero (v4 и v5): id → как назвать голосом и как представить.
SILERO_VOICES = {
    "aidar": ("айдар", "мужской"),
    "eugene": ("евгений", "мужской"),
    "baya": ("бая", "женский"),
    "kseniya": ("ксения", "женский"),
    "xenia": ("ксюша", "женский"),
}


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


class Pyttsx3Backend:
    name = "pyttsx3"

    def __init__(self, voice_hint: str):
        self.voice_hint = voice_hint.lower()
        self.voice_name: str | None = None
        self._engine = None

    def load(self) -> None:
        import pyttsx3  # type: ignore
        self._engine = pyttsx3.init()
        for v in self._engine.getProperty("voices") or []:
            meta = f"{v.id} {v.name} {getattr(v, 'languages', '')}".lower()
            if self.voice_hint in meta or any(k in meta for k in ("russian", "irina", "pavel")):
                self._engine.setProperty("voice", v.id)
                self.voice_name = v.name
                return
        log.warning("Русский голос не найден; установите его в Параметрах Windows → Время и язык → Речь")

    def apply(self, rate: int, volume: float) -> None:
        if self._engine is not None:
            self._engine.setProperty("rate", int(rate))
            self._engine.setProperty("volume", float(volume))

    def speak(self, sentence: str) -> None:
        self._engine.say(sentence)
        self._engine.runAndWait()

    def stop(self) -> None:
        if self._engine is not None:
            try:
                self._engine.stop()
            except Exception:
                pass


class SileroBackend:
    """Silero TTS v5/v4 (ru). Работает на CPU, чтобы не занимать VRAM рядом с LLM."""

    name = "silero"
    SAMPLE_RATE = 48000

    def __init__(self, model_path: str | Path, speaker: str = "aidar", threads: int = 4):
        self.model_path = Path(model_path)
        self.speaker = speaker
        self.threads = threads
        self.voice_name = f"Silero {speaker}"
        self.volume = 1.0
        self.speed = 1.0
        self._model = None
        # v5 различает омографы («з+амок/зам+ок»); v4 таких параметров не знает.
        self._homographs = True

    def load(self) -> None:
        if not self.model_path.is_file():
            raise FileNotFoundError(f"Нет модели Silero: {self.model_path}. "
                                    "Запустите scripts/download_models.py --silero")
        import torch  # type: ignore
        torch.set_num_threads(self.threads)
        importer = torch.package.PackageImporter(str(self.model_path))
        self._model = importer.load_pickle("tts_models", "model")
        self._model.to(torch.device("cpu"))
        # TorchScript оптимизирует модель после пары прогонов: без прогрева
        # первый ответ синтезируется ~4 с, после — ~0.3 с.
        for phrase in ("Готов.", "Проверка связи, всё работает."):
            self._model.apply_tts(text=phrase, speaker=self.speaker, sample_rate=self.SAMPLE_RATE)

    def apply(self, rate: int, volume: float) -> None:
        self.volume = float(volume)
        # Скорость: 185 (по умолчанию pyttsx3) ≈ нормальная речь Silero.
        self.speed = max(0.6, min(1.6, rate / 185))

    def synthesize(self, sentence: str):
        import numpy as np
        from .text_normalize import normalize_for_tts
        text = normalize_for_tts(sentence)
        if not text:
            return None
        kwargs = dict(text=text, speaker=self.speaker, sample_rate=self.SAMPLE_RATE,
                      put_accent=True, put_yo=True)
        if self._homographs:
            try:
                audio = self._model.apply_tts(**kwargs, put_stress_homo=True, put_yo_homo=True)
            except TypeError:  # модель v4
                self._homographs = False
        if not self._homographs:
            audio = self._model.apply_tts(**kwargs)
        audio = audio.numpy().astype(np.float32) * self.volume
        if abs(self.speed - 1.0) > 0.05:
            audio = _change_speed(audio, self.speed)
        return audio

    def speak(self, sentence: str) -> None:
        import sounddevice as sd  # type: ignore
        audio = self.synthesize(sentence)
        if audio is not None and audio.size:
            sd.play(audio, self.SAMPLE_RATE)
            sd.wait()

    def stop(self) -> None:
        try:
            import sounddevice as sd  # type: ignore
            sd.stop()
        except Exception:
            pass


def _change_speed(audio, speed: float):
    """Простое ускорение/замедление (с изменением высоты, но без зависимостей)."""
    import numpy as np
    n = int(len(audio) / speed)
    return np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)


def resolve_silero_model(path: str | Path | None) -> Path:
    """Путь к модели Silero. Если новой (v5) ещё нет, а старая v4 скачана — берём v4."""
    p = Path(path or "models/silero/v5_6_ru.pt")
    if not p.is_file():
        old = p.with_name("v4_ru.pt")
        if old.is_file():
            log.info("Модель %s не найдена, использую %s (скачайте новую: download_models.py "
                     "--only-silero)", p.name, old.name)
            return old
    return p


class Speaker:
    def __init__(self, engine: str = "pyttsx3", rate: int = 185, volume: float = 1.0,
                 voice_hint: str = "ru", silero_model: str | Path | None = None,
                 silero_speaker: str = "aidar"):
        self.engine_name = engine
        self.rate = rate
        self.volume = volume
        self.voice_hint = voice_hint
        self.silero_model = silero_model
        self.silero_speaker = silero_speaker
        self.backend = None
        self._lock = threading.Lock()
        self._interrupt = threading.Event()

    @property
    def voice_name(self) -> str | None:
        return getattr(self.backend, "voice_name", None)

    def load(self) -> None:
        if self.engine_name == "print":
            return
        if self.engine_name == "auto":
            import importlib.util
            has_silero = (self.silero_model is not None and Path(self.silero_model).is_file()
                          and importlib.util.find_spec("torch") is not None)
            self.engine_name = "silero" if has_silero else "pyttsx3"
        if self.engine_name == "silero":
            try:
                backend = SileroBackend(resolve_silero_model(self.silero_model),
                                        self.silero_speaker)
                backend.load()
                self.backend = backend
            except Exception as e:
                log.warning("Silero недоступен (%s), использую системный голос", e)
                self.engine_name = "pyttsx3"
        if self.engine_name == "pyttsx3":
            backend = Pyttsx3Backend(self.voice_hint)
            backend.load()
            self.backend = backend
        elif self.backend is None:
            raise ValueError(f"Неизвестный TTS-движок: {self.engine_name}")
        self.apply_settings()

    def apply_settings(self) -> None:
        if self.backend is not None:
            self.backend.apply(self.rate, self.volume)

    def set_voice(self, speaker: str) -> bool:
        """Сменить голос Silero на лету; False — сейчас системный голос, менять нечего."""
        if speaker not in SILERO_VOICES:
            raise ValueError(speaker)
        self.silero_speaker = speaker
        if isinstance(self.backend, SileroBackend):
            with self._lock:  # не посреди фразы
                self.backend.speaker = speaker
                self.backend.voice_name = f"Silero {speaker}"
            return True
        return False

    def interrupt(self) -> None:
        self._interrupt.set()
        if self.backend is not None:
            self.backend.stop()

    def say(self, text: str) -> None:
        if not text:
            return
        self._interrupt.clear()
        print(f"JARVIS: {text}", flush=True)
        if self.backend is None:
            return
        with self._lock:
            for sentence in split_sentences(text):
                if self._interrupt.is_set():
                    break
                self.backend.speak(sentence)
