"""Сквозные тесты голосового цикла с поддельными микрофоном, детектором и ASR.

Настоящими остаются: определение конца речи, ядро помощника, реестр,
подтверждения, напоминания. Подделаны только устройства и модели.
"""

import threading
import time

import numpy as np
import pytest

from conftest import FakeLLM, tool_call
from jarvis import main as main_mod
from jarvis.audio_input import VadConfig
from jarvis.main import App, VoiceParts
from jarvis.text_to_speech import Speaker

SR = 16000
BLOCK = SR * 30 // 1000
WAKE = 7777   # маркер: в этом блоке сказано «Джарвис»
STOP = 8888   # маркер: сказано «стоп» во время речи помощника


def silence(seconds):
    n = int(seconds * 1000 / 30)
    rng = np.random.default_rng(1)
    return [(rng.normal(0, 0.002, BLOCK) * 32768).astype(np.int16) for _ in range(n)]


def speech(seconds=0.6):
    t = np.arange(BLOCK) / SR
    blk = (0.1 * np.sin(2 * np.pi * 220 * t) * 32768).astype(np.int16)
    return [blk.copy() for _ in range(int(seconds * 1000 / 30))]


def marker(value):
    return [np.full(BLOCK, value, np.int16)]


class FakeMic:
    def __init__(self, blocks, delay=0.0):
        self._blocks = list(blocks)
        self.delay = delay  # пауза между блоками, чтобы имитировать реальное время
        self.muted = False
        self.error = None
        self.stopped = False

    def blocks(self, stop_event=None, timeout=0.5):
        while self._blocks:
            if stop_event is not None and stop_event.is_set():
                return
            if self.delay:
                time.sleep(self.delay)
            yield self._blocks.pop(0)
        self.error = "Запись закончилась"

    def mute(self):
        self.muted = True

    def unmute(self):
        self.muted = False

    def flush(self):
        pass

    def stop(self):
        self.stopped = True


class FakeWake:
    def __init__(self, value):
        self.value = value

    def wait(self, blocks):
        for b in blocks:
            if b[0] == self.value:
                return True
        return False

    def process(self, block):
        return block[0] == self.value

    def reset(self):
        pass


class FakeASR:
    def __init__(self, texts):
        self.texts = list(texts)

    def transcribe(self, audio):
        assert audio is not None and audio.size > 0
        item = self.texts.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def make_app(config, monkeypatch):
    monkeypatch.setattr(main_mod, "play_beep", lambda: None)
    config.set("assistant.follow_up_seconds", 1)
    config.set("assistant.barge_in", False)
    apps = []

    def factory(blocks, texts, llm_responses=()):
        app = App(config, voice=True)
        app.speaker = Speaker("print")
        app.assistant.llm = FakeLLM(llm_responses)
        launched = []
        app.registry.get("open_app").handler = lambda name: launched.append(name) or f"Запускаю {name}."
        vad = VadConfig(sample_rate=SR, block_ms=30, silence_ms_to_stop=600,
                        no_speech_timeout_seconds=2, min_speech_ms=150)
        parts = VoiceParts(FakeMic(blocks), FakeWake(WAKE), None, FakeASR(texts), vad, ["джарвис"])
        app.launched = launched
        app.parts = parts
        apps.append(app)
        return app

    yield factory
    for app in apps:
        app.close()


def run(app, timeout=10):
    t = threading.Thread(target=app.run_voice, args=(app.parts,), kwargs={"console": False},
                         daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive(), "голосовой цикл завис"


def test_wake_command_and_follow_up(make_app, capsys):
    blocks = (silence(0.5) + marker(WAKE) + speech() + silence(0.8)     # «Джарвис, открой блокнот»
              + speech() + silence(0.8)                                 # продолжение без «Джарвис»
              + silence(1.5))                                           # тишина → назад к ожиданию
    app = make_app(blocks, ["Джарвис, открой блокнот", "который час"])
    run(app)
    out = capsys.readouterr().out
    assert app.launched == ["блокнот"]
    assert "Вы: открой блокнот" in out          # «Джарвис» вырезан из команды
    assert "JARVIS: Сейчас" in out              # продолжение диалога сработало
    assert app.parts.mic.stopped


def test_silence_after_wake_returns_to_waiting(make_app, capsys):
    blocks = marker(WAKE) + silence(2.5) + marker(WAKE) + speech() + silence(0.8)
    app = make_app(blocks, ["открой блокнот"])
    run(app)
    assert app.launched == ["блокнот"]


def test_dangerous_action_confirmed_by_voice(make_app, config, capsys):
    from pathlib import Path
    target = Path(config.get("windows.search.allowed_dirs")[0]) / "old.txt"
    target.write_text("x")
    trashed = []
    blocks = marker(WAKE) + speech() + silence(0.8) + speech() + silence(0.8) + silence(1.5)
    app = make_app(blocks, ["удали файл old.txt", "подтверждаю"],
                   [tool_call("delete_file", path=str(target))])
    ctl_tool = app.registry.get("delete_file")
    ctl_tool.handler = lambda path: trashed.append(path) or "Файл перемещён в корзину."
    run(app)
    out = capsys.readouterr().out
    assert "Требуется подтверждение" in out
    assert trashed == [str(target)]


def test_asr_error_does_not_kill_assistant(make_app, capsys):
    blocks = (marker(WAKE) + speech() + silence(0.8)
              + marker(WAKE) + speech() + silence(0.8))
    app = make_app(blocks, [RuntimeError("CUDA упала"), "открой блокнот"])
    run(app)
    out = capsys.readouterr().out
    assert "Произошла ошибка, продолжаю работу." in out
    assert app.launched == ["блокнот"]


def test_too_many_errors_stop_loop(make_app, capsys):
    blocks = []
    for _ in range(6):
        blocks += marker(WAKE) + speech() + silence(0.8)
    app = make_app(blocks, [RuntimeError("сломано")] * 6)
    run(app)
    assert "Слишком много ошибок" in capsys.readouterr().out


def test_mute_by_voice_then_quit(make_app, capsys):
    blocks = marker(WAKE) + speech() + silence(0.8) + silence(5)
    app = make_app(blocks, ["выключи микрофон"])
    threading.Timer(1.0, app.quit).start()
    run(app)
    assert app.parts.mic.muted
    assert "Выключаю микрофон" in capsys.readouterr().out


def test_exit_by_voice(make_app, capsys):
    blocks = marker(WAKE) + speech() + silence(0.8) + silence(5) + marker(WAKE) + speech()
    app = make_app(blocks, ["заверши работу", "открой блокнот"])
    run(app)
    assert app.stop_event.is_set()
    assert app.launched == []


def test_reminder_wakes_idle_loop(make_app, capsys):
    blocks = silence(3)
    app = make_app(blocks, [])
    app.parts.mic.delay = 0.02  # ~2 секунды: фоновая проверка идёт раз в секунду
    app.reminders.add("полить цветы", 1)
    app.reminders.clock = lambda: time.time() + 5  # срок наступил, но не «пропущен»
    run(app)
    assert "Напоминание: полить цветы." in capsys.readouterr().out
