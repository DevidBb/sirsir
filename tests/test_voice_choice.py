"""Голос: Silero v5 (омографы), откат на v4, смена голоса голосом."""

import pytest

from jarvis import text_to_speech as tts
from jarvis.intents import Intent, IntentMatcher


class FakeTensor:
    def __init__(self, n):
        self.n = n

    def numpy(self):
        import numpy as np
        return np.ones(self.n, dtype=np.float32)


class FakeV5:
    def __init__(self):
        self.calls = []

    def apply_tts(self, text, speaker, sample_rate, put_accent, put_yo,
                  put_stress_homo=False, put_yo_homo=False):
        self.calls.append((speaker, put_stress_homo, put_yo_homo))
        return FakeTensor(100)


class FakeV4:
    def __init__(self):
        self.calls = []

    def apply_tts(self, text, speaker, sample_rate, put_accent, put_yo):
        self.calls.append(speaker)
        return FakeTensor(100)


def test_v5_uses_homographs():
    b = tts.SileroBackend("x.pt", "aidar")
    b._model = FakeV5()
    b.synthesize("Открой замок.")
    assert b._model.calls == [("aidar", True, True)]


def test_v4_falls_back_without_homographs():
    b = tts.SileroBackend("x.pt", "baya")
    b._model = FakeV4()
    assert b.synthesize("Привет.") is not None
    assert b.synthesize("Ещё раз.") is not None
    assert b._model.calls == ["baya", "baya"] and b._homographs is False


def test_resolve_model_prefers_v5_then_v4(tmp_path):
    v5 = tmp_path / "v5_6_ru.pt"
    assert tts.resolve_silero_model(v5) == v5          # ничего нет — путь из настроек
    (tmp_path / "v4_ru.pt").write_bytes(b"x")
    assert tts.resolve_silero_model(v5) == tmp_path / "v4_ru.pt"
    v5.write_bytes(b"x")
    assert tts.resolve_silero_model(v5) == v5


def test_speaker_set_voice_on_the_fly():
    s = tts.Speaker("silero")
    s.backend = tts.SileroBackend("x.pt", "aidar")
    assert s.set_voice("xenia") and s.backend.speaker == "xenia"
    with pytest.raises(ValueError):
        s.set_voice("robot")
    s.backend = object()  # системный голос — менять нечего
    assert s.set_voice("baya") is False


@pytest.mark.parametrize("phrase,voice", [
    ("смени голос", "next"),
    ("Другой голос.", "next"),
    ("женский голос", "female"),
    ("поставь мужской голос", "male"),
    ("голос бая", "baya"),
    ("поставь голос Евгений", "eugene"),
    ("голос ксюша", "xenia"),
])
def test_voice_intents(phrase, voice):
    assert IntentMatcher({}, {}).match(phrase) == Intent("set_voice", {"voice": voice})


@pytest.mark.parametrize("phrase", ["голос тише", "какой у тебя голос"])
def test_voice_intents_not_overmatched(phrase):
    intent = IntentMatcher({}, {}).match(phrase)
    assert intent is None or intent.tool != "set_voice"


def test_app_set_voice_tool(config, monkeypatch):
    """Инструмент set_voice в сборке приложения: смена, пол, сохранение в памяти."""
    from jarvis import main
    monkeypatch.setattr(main, "make_speaker", lambda cfg, voice=True: tts.Speaker("silero"))
    app = main.App(config, voice=False)
    app.speaker.backend = tts.SileroBackend("x.pt", "aidar")
    tool = app.registry.get("set_voice")
    assert "Евгений" in tool.handler(voice="next")
    assert app.speaker.backend.speaker == "eugene"
    assert "Бая" in tool.handler(voice="female")
    assert "Ксения" in tool.handler(voice="female")   # уже женский — следующий женский
    assert "Айдар" in tool.handler(voice="male")
    assert app.memory.get_setting("tts.silero_speaker") == "aidar"
    app.speaker.backend = object()
    assert "системный голос" in tool.handler(voice="baya")
    app.close()


def test_voices_demo_saves_choice(config, monkeypatch, tmp_path):
    from jarvis import main
    from jarvis.memory import Memory
    said = []

    class FakeSpeaker(tts.Speaker):
        def load(self):
            self.engine_name = "silero"
            self.backend = tts.SileroBackend("x.pt", "aidar")

        def say(self, text):
            said.append((self.backend.speaker, text))

    monkeypatch.setattr(tts, "Speaker", FakeSpeaker)
    assert main.voices_demo(config, choose=lambda prompt: "3") == 0
    assert [v for v, _ in said] == list(tts.SILERO_VOICES)
    mem = Memory(config.path("memory.db_path"))
    assert mem.get_setting("tts.silero_speaker") == "baya"
    mem.close()
