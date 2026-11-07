"""Один быстрый Джарвис: мгновенные приветствия, один основной «мозг», замер скорости ответа."""
import random
from types import SimpleNamespace

import pytest

from jarvis import checks
from jarvis import text_to_speech as tts
from jarvis.main import SINGLE_BRAIN_FLAG, App, apply_saved_settings, needs_local_ollama
from jarvis.memory import Memory
from jarvis.text_to_speech import Speaker
from test_assistant import build


@pytest.mark.parametrize("phrase", ["привет", "Привет!", "привет, Джарвис", "Джарвис привет",
                                    "здравствуйте", "добрый вечер", "доброе утро", "хай"])
def test_greeting_answers_instantly_without_model(config, phrase):
    a = build(config, [])
    reply = a.handle(phrase)
    assert reply is not None and reply.text
    assert reply.expect_followup  # после «привет» слушаем команду без нового «Джарвис»
    assert a.fake.requests == []


@pytest.mark.parametrize("phrase", ["как дела", "привет, как дела?", "Джарвис, как ты?",
                                    "добрый день как поживаешь"])
def test_how_are_you_answers_without_model(config, phrase):
    a = build(config, [])
    a.rng = random.Random(1)
    reply = a.handle(phrase)
    assert any(s in reply.text for s in ("Всё отлично", "Всё хорошо", "Отлично, готов"))
    assert a.fake.requests == []


@pytest.mark.parametrize("phrase,night", [("пока", False), ("пока, Джарвис", False),
                                          ("до свидания", False), ("доброй ночи", True),
                                          ("спокойной ночи", True)])
def test_farewell_ends_dialog(config, phrase, night):
    a = build(config, [])
    reply = a.handle(phrase)
    assert not reply.expect_followup  # прощание — не повод слушать дальше
    assert ("ночи" in reply.text) == night and "Привет" not in reply.text
    assert a.fake.requests == []


@pytest.mark.parametrize("phrase", ["привет, открой калькулятор", "как ты это сделал",
                                    "переведи привет на английский", "напиши другу привет",
                                    "здорово", "пока не надо"])
def test_not_only_greeting_goes_further(config, phrase):
    # «здорово» после ответа чаще значит «отлично» — пусть отвечает модель с учётом разговора.
    a = build(config, [])
    assert a._greeting(phrase) is None


def test_greeting_command_still_runs(config):
    a = build(config, [])
    a.handle("привет, открой калькулятор")
    assert a.launched == ["calc.exe"]


def test_greeting_varies(config):
    a = build(config, [])
    a.rng = random.Random(7)
    answers = {a._greeting("привет").text for _ in range(30)}
    assert len(answers) > 1


# --- один основной мозг: как при настоящем запуске (main → apply_saved_settings → App) --------

def saved(config, provider):
    memory = Memory(config.path("memory.db_path"))
    if provider is not None:
        memory.set_setting("llm.provider", provider)
    memory.close()


def stored(config, key):
    memory = Memory(config.path("memory.db_path"))
    try:
        return memory.get_setting(key)
    finally:
        memory.close()


def startup(config):
    """Порядок из main(): сначала memory.db поверх config.toml, потом App."""
    config.set("llm.provider", "gemini")  # значение из config.toml (conftest ставит ollama)
    apply_saved_settings(config)
    return config.get("llm.provider")


def test_saved_team_switches_to_main_brain_once(config, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    saved(config, "team")
    assert startup(config) == "gemini"
    assert stored(config, "llm.provider") == "gemini" and stored(config, SINGLE_BRAIN_FLAG)
    # Дальше выбор в HUD снова сохраняется и не перетирается.
    saved(config, "ollama")
    assert startup(config) == "ollama"


def test_app_after_startup_uses_main_brain(config, monkeypatch):
    from jarvis.gemini import GeminiClient
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    saved(config, "team")
    startup(config)
    app = App(config, voice=False)  # App ещё раз применяет memory.db — там уже gemini
    try:
        assert config.get("llm.provider") == "gemini" and isinstance(app.llm, GeminiClient)
    finally:
        app.close()


def test_no_gemini_key_keeps_saved_choice(config, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    saved(config, "team")
    assert startup(config) == "team"
    assert not stored(config, SINGLE_BRAIN_FLAG)
    # Ключ появился — переход случится при следующем запуске.
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert startup(config) == "gemini"


def test_nothing_saved_just_marks_done(config, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    saved(config, None)
    assert startup(config) == "gemini"
    assert stored(config, "llm.provider") is None and stored(config, SINGLE_BRAIN_FLAG)


def by_name(results, name):
    return [r for r in results if r.name == name]


def test_no_gemini_key_does_not_block_start(config, monkeypatch):
    # run.bat / run_text.bat выходят при обязательном FAIL: без ключа это лишь предупреждение,
    # а обязательной становится Ollama, которая и будет отвечать.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    config.set("llm.provider", "gemini")
    [gem] = checks.check_gemini(config)
    assert not gem.ok and not gem.required and "Ollama" in gem.detail
    monkeypatch.setattr(checks.OllamaClient, "list_models", lambda self: ["qwen3:8b"])
    monkeypatch.setattr(checks.OllamaClient, "has_model", lambda self: True)
    assert all(r.required for r in checks.check_ollama(config))
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert not any(r.required for r in checks.check_ollama(config))


def test_ollama_autostarts_only_when_it_answers(config, monkeypatch):
    config.set("llm.provider", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert not needs_local_ollama(config)  # облачный мозг — Ollama не нужна
    monkeypatch.delenv("GEMINI_API_KEY")
    assert needs_local_ollama(config)      # без ключа отвечает Ollama — запустить её


# --- замер скорости -------------------------------------------------------------------

class FakeBackend:
    def __init__(self, *args):
        self.spoken = []

    def load(self):
        pass

    def speak(self, sentence):
        self.notify()
        self.spoken.append(sentence)

    def apply(self, rate, volume):
        pass

    def stop(self):
        pass


def test_speaker_load_wires_audio_timing(monkeypatch):
    monkeypatch.setattr(tts, "Pyttsx3Backend", FakeBackend)
    sp = Speaker(engine="pyttsx3")
    sp.load()  # load() сам подключает отметку первого звука
    times = iter([1.0, 2.0, 3.0])
    sp.clock = lambda: next(times)
    sp.mark_timing()
    sp.say("Первое. Второе.")
    assert sp.first_audio_at == 1.0  # второе предложение не сдвигает отметку
    sp.mark_timing()
    assert sp.first_audio_at is None


def test_report_timing_counts_from_end_of_phrase(capsys):
    events = []
    app = SimpleNamespace(speaker=SimpleNamespace(first_audio_at=11.2), observer=events.append)
    App._report_timing(app, 700, t_end=10.0, t_text=10.3, t_done=13.0)
    ev = events[-1]
    assert (ev["type"], ev["stt"], ev["voice"], ev["total"], ev["silence"]) == ("timing", 0.3, 1.2, 3.0, 0.7)
    out = capsys.readouterr().out
    assert "голос через 1.20 с" in out and "весь ответ 3.00 с" in out


def test_silero_marks_audio_after_synthesis(monkeypatch):
    import sys
    import numpy as np
    order = []
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(
        play=lambda audio, rate: order.append("play"), wait=lambda: None, stop=lambda: None))
    backend = tts.SileroBackend.__new__(tts.SileroBackend)
    backend.synthesize = lambda sentence: order.append("synth") or np.ones(10, dtype=np.float32)
    backend.notify = lambda: order.append("notify")
    backend.speak("Привет.")
    assert order == ["synth", "notify", "play"]  # отметка — после синтеза, прямо перед звуком
