"""Детектор фразы пробуждения «Джарвис».

Используется Vosk с ограниченной грамматикой: распознаватель знает только
слова-триггеры и «[unk]», поэтому он лёгкий (~50 МБ модели, только CPU)
и почти не даёт ложных срабатываний на обычную речь.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable

import numpy as np

from .security import contains_phrase

log = logging.getLogger(__name__)


class VoskWakeWord:
    def __init__(self, model_path: str | Path, phrases: list[str], sample_rate: int = 16000):
        self.model_path = Path(model_path)
        self.phrases = phrases
        self.sample_rate = sample_rate
        self._model = None
        self._rec = None

    def load(self) -> None:
        if not self.model_path.is_dir():
            raise FileNotFoundError(
                f"Модель Vosk не найдена: {self.model_path}. Запустите scripts/download_models.py")
        import vosk  # type: ignore
        vosk.SetLogLevel(-1)
        self._model = vosk.Model(str(self.model_path))
        self.reset()

    def reset(self) -> None:
        import vosk  # type: ignore
        grammar = json.dumps(self.phrases + ["[unk]"], ensure_ascii=False)
        self._rec = vosk.KaldiRecognizer(self._model, self.sample_rate, grammar)

    def process(self, block: np.ndarray) -> bool:
        """Подаёт блок int16; True — фраза пробуждения услышана."""
        data = block.astype(np.int16).tobytes()
        if self._rec.AcceptWaveform(data):
            text = json.loads(self._rec.Result()).get("text", "")
        else:
            text = json.loads(self._rec.PartialResult()).get("partial", "")
        if text and contains_phrase(text, self.phrases):
            self.reset()
            return True
        return False

    def wait(self, blocks: Iterable[np.ndarray]) -> bool:
        """Блокирует до срабатывания; False — поток блоков закончился."""
        for block in blocks:
            if self.process(block):
                return True
        return False


def strip_wake_phrase(text: str, phrases: list[str]) -> str:
    """«Джарвис, открой блокнот» → «открой блокнот»."""
    words = text.strip().split()
    if words and contains_phrase(words[0], phrases):
        return " ".join(words[1:]).lstrip(",.!: ")
    return text.strip()
