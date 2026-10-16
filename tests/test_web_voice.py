"""Локальный голос в режиме HUD: события в интерфейс, «Слушать», микрофон, общий замок."""

import threading

from conftest import FakeLLM
from jarvis.web_app import Hub, Runtime
import test_voice_loop
from test_voice_loop import WAKE, marker, run, silence, speech

make_app = test_voice_loop.make_app  # та же фикстура: поддельные микрофон, детектор и ASR


def events(hub, kind=None):
    return [e for e in hub.history if kind is None or e["type"] == kind]


def test_voice_command_appears_in_hud(make_app):
    blocks = silence(0.3) + marker(WAKE) + speech() + silence(0.8) + silence(1.5)
    app = make_app(blocks, ["Джарвис, открой блокнот"])
    hub = Hub()
    Runtime(app, hub)
    run(app)
    tasks = events(hub, "task")
    assert tasks and tasks[0]["text"] == "открой блокнот" and tasks[0]["source"] == "voice"
    replies = events(hub, "reply")
    assert replies[0]["task"] == tasks[0]["task"] and "блокнот" in replies[0]["text"]
    states = [e["state"] for e in events(hub, "state")]
    assert "listening" in states and "thinking" in states
    tools = [e["name"] for e in events(hub, "tool_start")]
    assert tools == ["open_app"]
    assert app.launched == ["блокнот"]
    assert events(hub, "voice")[-1]["enabled"] is False  # цикл завершился


def test_listen_button_skips_wake_word(make_app):
    blocks = speech() + silence(0.8) + silence(1.5)  # «Джарвис» не сказано
    app = make_app(blocks, ["открой блокнот"])
    Runtime(app, Hub())
    app.force_listen.set()  # кнопка «Слушать» до начала
    run(app)
    assert app.launched == ["блокнот"]


def test_listen_now_unmutes_and_wakes(make_app):
    app = make_app([], [])
    app.mic = app.parts.mic
    app.mic.muted = True
    app.listen_now()
    assert not app.mic.muted and app.force_listen.is_set() and app.wake_interrupt.is_set()


def test_voice_commands_require_running_voice(make_app):
    app = make_app([], [])
    hub = Hub()
    runtime = Runtime(app, hub)
    runtime.voice_command("listen")
    assert "не запущен" in events(hub, "notice")[-1]["text"]
    assert not app.force_listen.is_set()


def test_mic_toggle_from_hud(make_app):
    app = make_app([], [])
    hub = Hub()
    runtime = Runtime(app, hub)
    app.mic = app.parts.mic
    app.voice_active = True
    runtime.voice_command("mic", False)
    assert app.mic.muted and events(hub, "voice")[-1]["muted"] is True
    runtime.voice_command("mic", True)
    assert not app.mic.muted


def test_chat_waits_while_voice_command_runs(make_app):
    import time
    app = make_app([], [])
    app.assistant.llm = FakeLLM([{"role": "assistant", "content": "ответ"}])
    hub = Hub()
    runtime = Runtime(app, hub)
    held, release = threading.Event(), threading.Event()

    def voice_turn():  # голосовая команда держит ядро
        with app.handle_lock:
            held.set()
            release.wait(2)
    threading.Thread(target=voice_turn, daemon=True).start()
    held.wait(1)
    runtime.submit("расскажи что-нибудь интересное")
    time.sleep(0.3)
    assert not events(hub, "reply")  # чат ждёт
    release.set()
    for _ in range(50):
        if events(hub, "reply"):
            break
        time.sleep(0.05)
    assert events(hub, "reply")[-1]["text"] == "ответ"
    runtime.stop()


def test_reminder_reaches_hud(make_app):
    app = make_app([], [])
    hub = Hub()
    Runtime(app, hub)
    app._on_reminder("Напоминаю: чай")
    assert events(hub, "notice")[-1]["text"] == "Напоминаю: чай"
