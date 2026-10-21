import numpy as np
import pytest

from jarvis import text_to_speech as tts
from jarvis.text_normalize import normalize_for_tts, number_to_words


@pytest.mark.parametrize("n,words", [
    (0, "ноль"), (7, "семь"), (11, "одиннадцать"), (42, "сорок два"), (100, "сто"),
    (215, "двести пятнадцать"), (1000, "тысяча"), (2026, "две тысячи двадцать шесть"),
    (21000, "двадцать одна тысяча"), (1_000_000, "один миллион"), (-5, "минус пять"),
])
def test_number_to_words(n, words):
    assert number_to_words(n) == words


@pytest.mark.parametrize("text,spoken", [
    ("Сейчас 12:43", "Сейчас двенадцать сорок три"),
    ("в 09:05", "в девять ноль пять"),
    ("в 18:00", "в восемнадцать ноль ноль"),
    ("загружен на 21%", "загружен на двадцать один процент"),
    ("на 42 %", "на сорок два процента"),
    ("2.5 гигабайта", "два и пять гигабайта"),
    ("**жирный** `код`", "жирный код"),
    ("Tom & Jerry", "Tom и Jerry"),
])
def test_normalize_for_tts(text, spoken):
    assert normalize_for_tts(text) == spoken


class FakeTensor:
    def __init__(self, n):
        self.n = n

    def numpy(self):
        return np.ones(self.n, dtype=np.float32)


class FakeSilero:
    def __init__(self):
        self.texts = []

    def apply_tts(self, text, speaker, sample_rate, put_accent, put_yo):
        self.texts.append((text, speaker))
        return FakeTensor(1000)


def test_silero_synthesize_normalizes_and_scales():
    b = tts.SileroBackend("x.pt", speaker="baya")
    b._model = FakeSilero()
    b.apply(rate=185, volume=0.5)
    audio = b.synthesize("Сейчас 12:30.")
    assert b._model.texts == [("Сейчас двенадцать тридцать.", "baya")]
    assert len(audio) == 1000 and audio.max() == pytest.approx(0.5)
    b.apply(rate=370, volume=1.0)  # в 2 раза быстрее (ограничение 1.6)
    assert len(b.synthesize("раз")) == int(1000 / 1.6)
    assert b.synthesize("***") is None


def test_silero_missing_model_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(tts.Pyttsx3Backend, "load", lambda self: None)
    s = tts.Speaker("silero", silero_model=tmp_path / "нет.pt")
    s.load()
    assert s.engine_name == "pyttsx3"
    assert isinstance(s.backend, tts.Pyttsx3Backend)


class RecordingBackend:
    voice_name = "fake"

    def __init__(self, speaker, interrupt_after=None):
        self.spoken = []
        self.speaker = speaker
        self.interrupt_after = interrupt_after
        self.stopped = False

    def apply(self, rate, volume):
        pass

    def speak(self, sentence):
        self.spoken.append(sentence)
        if len(self.spoken) == self.interrupt_after:
            self.speaker.interrupt()

    def stop(self):
        self.stopped = True


def test_interrupt_between_sentences(capsys):
    s = tts.Speaker("print")
    s.backend = RecordingBackend(s, interrupt_after=1)
    s.say("Первое. Второе. Третье.")
    assert s.backend.spoken == ["Первое."]
    assert s.backend.stopped
    s.backend.interrupt_after = None
    s.say("Снова. Говорю.")  # после прерывания следующая фраза звучит целиком
    assert s.backend.spoken[-2:] == ["Снова.", "Говорю."]


def test_auto_engine_picks_available(tmp_path, monkeypatch):
    monkeypatch.setattr(tts.Pyttsx3Backend, "load", lambda self: None)
    s = tts.Speaker("auto", silero_model=tmp_path / "нет.pt")
    s.load()
    assert s.engine_name == "pyttsx3"


def test_speech_envelope_follows_loudness():
    sr = 48000
    t = np.arange(sr) / sr
    quiet, loud = 0.02 * np.sin(2 * np.pi * 220 * t[:sr // 2]), 0.6 * np.sin(2 * np.pi * 220 * t[:sr // 2])
    env = tts.speech_envelope(np.concatenate([quiet, loud, np.zeros(sr // 4)]).astype(np.float32), sr)
    assert len(env) == int(1.25 / tts.ENVELOPE_FRAME)
    assert max(env[:20]) < 0.1 and min(env[30:45]) > 0.9 and max(env[-10:]) == 0.0  # тихо → громко → пауза
    assert tts.speech_envelope(np.zeros(10, np.float32), sr) == []


def test_silero_level_is_the_playing_envelope(monkeypatch):
    b = tts.SileroBackend("x.pt")
    now = [100.0]
    monkeypatch.setattr(tts.time, "monotonic", lambda: now[0])
    assert b.level() == 0.0  # ничего не звучит
    b.playing = (100.0, 0.02, [0.1, 0.9, 0.4])
    now[0] = 100.025
    assert b.level() == 0.9
    now[0] = 100.5
    assert b.level() == 0.0  # фраза кончилась
    b.stop()
    assert b.playing is None
    s = tts.Speaker("print")
    assert s.speech_level() is None  # без движка — сигнала нет, HUD покажет анимацию состояния
    s.backend = b
    b.playing = (now[0], 0.02, [0.7])
    assert s.speech_level() == 0.7
