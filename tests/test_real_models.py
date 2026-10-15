"""Проверки на настоящих моделях (Silero → Vosk/Whisper, Ollama).

Медленные (минуты), поэтому запускаются только явно:
    set JARVIS_REAL_TESTS=1            (Windows; в Linux: export JARVIS_REAL_TESTS=1)
    python -m pytest tests/test_real_models.py -v
Пропускаются, если моделей или пакетов нет.
Для LLM-теста нужна запущенная Ollama; модель берётся из JARVIS_TEST_MODEL
или config.toml.
"""

import importlib.util
import os
import tempfile
from pathlib import Path

import numpy as np
import pytest

from jarvis.config import Config

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(os.environ.get("JARVIS_REAL_TESTS") != "1",
                                reason="медленные тесты: JARVIS_REAL_TESTS=1")
CFG = Config.load(ROOT / "config.toml")


def _have(*modules):
    return all(importlib.util.find_spec(m) for m in modules)


needs_speech = pytest.mark.skipif(
    not (_have("torch", "vosk", "faster_whisper")
         and CFG.path("tts.silero_model_path").is_file()
         and CFG.path("wake_word.vosk_model_path").is_dir()
         and CFG.path("speech_recognition.models_dir").is_dir()),
    reason="нужны Silero, Vosk и Whisper (scripts/download_models.py --silero)")


@pytest.fixture(scope="module")
def voice():
    from jarvis.text_to_speech import SileroBackend
    tts = SileroBackend(CFG.path("tts.silero_model_path"), "eugene")
    tts.load()
    tts.apply(185, 1.0)
    rng = np.random.default_rng(0)

    def say16(text, speaker="eugene", pad=0.5):
        tts.speaker = speaker
        a = tts.synthesize(text)
        n = len(a) // 3
        a = a[: n * 3].reshape(n, 3).mean(axis=1)
        p = rng.normal(0, .003, int(16000 * pad)).astype(np.float32)
        return np.concatenate([p, a, p]).astype(np.float32)
    return say16


@pytest.fixture(scope="module")
def wake():
    from jarvis.wake_word import VoskWakeWord
    w = VoskWakeWord(CFG.path("wake_word.vosk_model_path"), CFG.get("wake_word.phrases"))
    w.load()
    return w


@pytest.fixture(scope="module")
def asr():
    from jarvis.speech_recognition import SpeechRecognizer
    r = SpeechRecognizer(CFG.get("speech_recognition.model"), CFG.get("speech_recognition.device"),
                         models_dir=CFG.path("speech_recognition.models_dir"))
    r.load()
    return r


def heard(det, audio):
    det.reset()
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    return det.wait(pcm[i:i + 480] for i in range(0, len(pcm) - 480, 480))


@needs_speech
@pytest.mark.parametrize("speaker", ["aidar", "baya", "eugene", "kseniya", "xenia"])
def test_wake_word_real(voice, wake, speaker):
    assert heard(wake, voice("Джарвис, открой блокнот.", speaker))
    false = [t for t in ("Мы поедем в Париж.", "Где мои ключи?", "Чарльз Дарвин написал книгу.")
             if heard(wake, voice(t, speaker))]
    assert false == []


@needs_speech
def test_barge_in_not_triggered_by_own_voice(voice, wake):
    stop = wake.with_phrases(CFG.get("assistant.interrupt_phrases"))
    assert not heard(stop, voice("Таймер на пять минут запущен. Хорошо, отменяю.", "aidar"))
    assert heard(stop, voice("Стоп.", "baya"))


@needs_speech
@pytest.mark.parametrize("phrase,expected", [
    ("Джарвис, открой блокнот.", "открой блокнот"),
    ("Поставь таймер на пять минут.", "таймер на 5 минут"),
    ("Который час?", "который час"),
])
def test_recognition_real(voice, asr, phrase, expected):
    from jarvis.security import normalize
    from jarvis.wake_word import strip_wake_phrase
    text = strip_wake_phrase(asr.transcribe(voice(phrase)), CFG.get("wake_word.phrases"))
    assert expected in normalize(text)


@needs_speech
def test_no_hallucinations_on_noise(asr):
    rng = np.random.default_rng(1)
    assert asr.transcribe(rng.normal(0, 0.02, 48000).astype(np.float32)) == ""
    assert asr.transcribe(np.zeros(32000, np.float32)) == ""


def _ollama_model():
    from jarvis.llm import LLMError, OllamaClient
    model = os.environ.get("JARVIS_TEST_MODEL", CFG.get("llm.model"))
    try:
        client = OllamaClient(CFG.get("llm.base_url"), model)
        return model if client.has_model() else None
    except LLMError:
        return None


@pytest.mark.skipif(_ollama_model() is None, reason="нужна запущенная Ollama с моделью")
def test_llm_real_tool_call_and_stream():
    from jarvis.main import App
    cfg = Config.load(ROOT / "config.toml")
    cfg.set("llm.model", _ollama_model())
    cfg.set("memory.db_path", str(Path(tempfile.mkdtemp()) / "m.db"))
    app = App(cfg, voice=False)
    try:
        app.assistant.intents.match = lambda text: None  # пусть решает модель
        called = []
        app.registry.get("set_timer").handler = lambda minutes=0, seconds=0: (
            called.append(int(minutes) * 60 + int(seconds)) or "Таймер запущен.")
        reply = app.assistant.handle("Поставь таймер на три минуты")
        assert called and called[0] in (180, 3), reply
        sentences = []
        reply = app.assistant.handle("Кто написал роман «Война и мир»? Ответь одним предложением.",
                                     on_sentence=lambda s: sentences.append(s) or True)
        assert reply.spoken and sentences
        assert "толст" in " ".join(sentences).lower()
        assert app.llm.last_stats.get("prompt_eval_count", 0) < int(cfg.get("llm.num_ctx"))
    finally:
        app.close()
