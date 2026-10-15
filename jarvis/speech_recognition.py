"""Распознавание русской речи через faster-whisper с откатом на CPU.

Whisper обучен на субтитрах и на шуме/тишине «дописывает» типичные титры:
«Продолжение следует…», «Редактор субтитров А.Семкин Корректор А.Егорова».
Такие фразы отфильтровываются, как и сегменты, которые сама модель считает
не-речью. Встроенный Silero VAD (входит в faster-whisper, без загрузок)
дополнительно вырезает паузы.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np

from .security import normalize

log = logging.getLogger(__name__)

# Подстроки, которые выдают галлюцинацию (после normalize()).
HALLUCINATION_MARKERS = (
    "субтитр", "корректор", "редактор субтитров", "dimatorzok", "продолжение следует",
    "спасибо за просмотр", "подписывайтесь на канал", "ставьте лайки", "подпишись на канал",
    "смотрите продолжение", "до встречи в следующем видео", "amara org", "игорь негода",
)
# Фразы, которые галлюцинация, только если это вся реплика целиком.
HALLUCINATION_EXACT = {"спасибо", "пока", "до свидания", "угу", "ага", "и", "а", "ну", "хм"}


def is_hallucination(text: str) -> bool:
    norm = normalize(text)
    if not norm:
        return True
    if any(m in norm for m in HALLUCINATION_MARKERS):
        return True
    return norm in HALLUCINATION_EXACT


class SpeechRecognizer:
    def __init__(self, model: str = "small", device: str = "cuda",
                 compute_type_gpu: str = "float16", compute_type_cpu: str = "int8",
                 models_dir: str | Path | None = None, language: str = "ru", beam_size: int = 1,
                 hotwords: str = "Джарвис"):
        self.model_name = model
        self.preferred_device = device
        self.compute_type_gpu = compute_type_gpu
        self.compute_type_cpu = compute_type_cpu
        self.models_dir = str(models_dir) if models_dir else None
        self.language = language
        self.beam_size = beam_size
        self.hotwords = hotwords
        self._model = None
        self.device: str | None = None

    def _load(self, device: str):
        from faster_whisper import WhisperModel  # тяжёлый импорт — по требованию
        compute = self.compute_type_gpu if device == "cuda" else self.compute_type_cpu
        model = WhisperModel(self.model_name, device=device, compute_type=compute,
                             download_root=self.models_dir)
        log.info("Whisper %s загружен на %s (%s)", self.model_name, device, compute)
        return model

    def load(self) -> str:
        """Загружает модель; при ошибке GPU — CPU. Возвращает устройство."""
        if self._model is not None:
            return self.device or "cpu"
        if self.preferred_device == "cuda":
            try:
                self._model = self._load("cuda")
                self.device = "cuda"
                return self.device
            except Exception as e:  # нет CUDA/cuDNN, не хватает VRAM и т.п.
                log.warning("Whisper на GPU недоступен (%s), переключаюсь на CPU", e)
        self._model = self._load("cpu")
        self.device = "cpu"
        return self.device

    def _fallback_to_cpu(self) -> None:
        self._model = None
        self.preferred_device = "cpu"
        self.load()

    def transcribe(self, audio: np.ndarray) -> str:
        if audio is None or audio.size == 0:
            return ""
        self.load()
        try:
            text = self._run(audio)
        except RuntimeError as e:
            if self.device != "cuda":
                raise
            log.warning("Ошибка GPU при распознавании (%s), переключаюсь на CPU", e)
            self._fallback_to_cpu()
            text = self._run(audio)
        if is_hallucination(text):
            if text:
                log.info("Отброшена галлюцинация Whisper: %r", text)
            return ""
        return text

    def _run(self, audio: np.ndarray) -> str:
        segments, _info = self._model.transcribe(
            audio.astype(np.float32), language=self.language, beam_size=self.beam_size,
            vad_filter=True, vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False, hotwords=self.hotwords or None)
        kept = []
        for s in segments:
            # Модель сама считает сегмент тишиной и не уверена в тексте — выбрасываем.
            if s.no_speech_prob > 0.6 and s.avg_logprob < -1.0:
                continue
            kept.append(s.text.strip())
        return re.sub(r"\s+", " ", " ".join(kept)).strip()
