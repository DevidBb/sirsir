"""Распознавание русской речи через faster-whisper с откатом на CPU."""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


class SpeechRecognizer:
    def __init__(self, model: str = "small", device: str = "cuda",
                 compute_type_gpu: str = "float16", compute_type_cpu: str = "int8",
                 models_dir: str | Path | None = None, language: str = "ru", beam_size: int = 1):
        self.model_name = model
        self.preferred_device = device
        self.compute_type_gpu = compute_type_gpu
        self.compute_type_cpu = compute_type_cpu
        self.models_dir = str(models_dir) if models_dir else None
        self.language = language
        self.beam_size = beam_size
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
            return self._run(audio)
        except RuntimeError as e:
            if self.device != "cuda":
                raise
            log.warning("Ошибка GPU при распознавании (%s), переключаюсь на CPU", e)
            self._fallback_to_cpu()
            return self._run(audio)

    def _run(self, audio: np.ndarray) -> str:
        segments, _info = self._model.transcribe(
            audio.astype(np.float32), language=self.language, beam_size=self.beam_size,
            vad_filter=False, condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in segments).strip()
