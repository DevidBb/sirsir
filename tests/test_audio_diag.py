"""Диагностика звука в HUD: микрофоны, смена на лету, проверка записи и голоса."""

import sys
import threading
import types

import numpy as np
import pytest

from jarvis import audio_diag
from jarvis.audio_input import Microphone


class FakeSD:
    default = types.SimpleNamespace(device=(2, 0))

    @staticmethod
    def query_hostapis():
        return [{"name": "MME"}, {"name": "Windows WASAPI"}]

    @staticmethod
    def query_devices(dev=None, kind=None):
        devices = [{"name": "Динамики", "max_input_channels": 0, "hostapi": 0, "default_samplerate": 48000},
                   {"name": "Микрофон (USB Audio)", "max_input_channels": 1, "hostapi": 1,
                    "default_samplerate": 48000},
                   {"name": "Микрофон (Realtek)", "max_input_channels": 2, "hostapi": 0,
                    "default_samplerate": 44100},
                   {"name": "CABLE Output (VB-Audio Virtual Cable)", "max_input_channels": 2, "hostapi": 0,
                    "default_samplerate": 44100}]
        return devices if dev is None else devices[dev if dev is not None else 2]


def test_list_microphones_marks_default_and_virtual(monkeypatch):
    monkeypatch.setattr(audio_diag, "_sd", lambda: FakeSD)
    info = audio_diag.list_microphones("Микрофон (USB Audio)|Windows WASAPI")
    names = [(d["name"], d["default"], d["virtual"]) for d in info["devices"]]
    assert names == [("Микрофон (USB Audio)", False, False), ("Микрофон (Realtek)", True, False),
                     ("CABLE Output (VB-Audio Virtual Cable)", False, True)]
    assert info["devices"][0]["spec"] == "Микрофон (USB Audio)|Windows WASAPI" and not info["error"]


def test_list_microphones_without_sounddevice(monkeypatch):
    def broken():
        raise RuntimeError("sounddevice недоступен: нет PortAudio")
    monkeypatch.setattr(audio_diag, "_sd", broken)
    info = audio_diag.list_microphones()
    assert info["devices"] == [] and "PortAudio" in info["error"]


def test_microphone_test_gives_verdict(monkeypatch):
    monkeypatch.setattr(audio_diag, "_sd", lambda: FakeSD)
    rng = np.random.default_rng(0)
    noise = (rng.normal(0, 0.002, 16000) * 32768).astype(np.int16)
    t = np.arange(48000) / 16000
    speech = (0.2 * np.sin(2 * np.pi * 200 * t) * 32768).astype(np.int16)
    result = audio_diag.test_microphone(None, record=lambda secs: np.concatenate([noise, speech]))
    assert result["verdict"] == "хорошо" and result["snr"] > 10


class Stream:
    opened = []
    fail_on = "нет"

    def __init__(self, samplerate, blocksize, callback, device=None, **kw):
        if device == Stream.fail_on:
            raise RuntimeError("Device unavailable")
        Stream.opened.append(device)
        self.callback, self.active, self.device = callback, False, device

    def start(self):
        self.active = True

    def stop(self):
        self.active = False

    def close(self):
        pass


@pytest.fixture
def fake_stream(monkeypatch):
    Stream.opened, Stream.fail_on = [], "нет"
    sd = types.SimpleNamespace(InputStream=Stream, query_devices=FakeSD.query_devices,
                               query_hostapis=FakeSD.query_hostapis)
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    return sd


def test_switch_microphone_keeps_queue(fake_stream):
    mic = Microphone(16000, 30, "Микрофон (Realtek)|MME")
    mic.start()
    gen = mic.blocks(timeout=0.05)
    mic.switch("Микрофон (USB Audio)|Windows WASAPI")
    assert Stream.opened == [2, 1] and mic.error is None
    mic._stream.callback(np.full((480, 1), 3000, np.int16), 480, None, None)
    assert next(gen)[0] == 3000 and mic.level > 0.05


def test_switch_failure_restores_previous(fake_stream):
    mic = Microphone(16000, 30, "Микрофон (Realtek)|MME")
    mic.start()
    Stream.fail_on = 1
    with pytest.raises(RuntimeError):
        mic.switch("Микрофон (USB Audio)|Windows WASAPI")
    assert mic.device == 2 and mic._stream.active and Stream.opened == [2, 2]


def test_blocks_wait_during_switch(fake_stream):
    mic = Microphone(16000, 30)
    mic.start()
    with mic._switch_lock:
        mic._stream = None  # поток временно закрыт
        got = []
        t = threading.Thread(target=lambda: got.append(next(mic.blocks(timeout=0.05), None)))
        t.start()
        t.join(0.3)
        assert t.is_alive() and mic.error is None  # генератор не завершился с ошибкой
    mic.start()
    mic._stream.callback(np.ones((480, 1), np.int16), 480, None, None)
    t.join(1)
    assert got and got[0] is not None


# --- API ---
starlette = pytest.importorskip("starlette")
pytest.importorskip("httpx")


@pytest.fixture
def client(config, monkeypatch):
    from starlette.testclient import TestClient
    from jarvis.main import App
    from jarvis.web_app import Hub, Runtime, create_app
    monkeypatch.setattr(audio_diag, "_sd", lambda: FakeSD)
    config.set("browser.enabled", False)
    app = App(config, voice=False)
    runtime = Runtime(app, Hub())
    with TestClient(create_app(runtime, 8765), base_url="http://127.0.0.1:8765") as c:
        yield c, app
    runtime.stop()
    app.close()


def test_audio_api_without_voice(client):
    c, app = client
    info = c.get("/api/audio").json()
    assert len(info["devices"]) == 3 and info["voice"]["enabled"] is False
    assert c.get("/api/audio/level").json() == {"active": False, "muted": False, "level": 0.0, "speech": None}
    assert not c.post("/api/audio/speak", json={}).json()["ok"]
    r = c.post("/api/audio/device", json={"spec": "Микрофон (USB Audio)|Windows WASAPI"}).json()
    assert r["ok"] and not r["applied"]
    assert app.memory.get_setting("audio.input_device") == "Микрофон (USB Audio)|Windows WASAPI"


def test_audio_api_with_running_voice(client):
    c, app = client

    class Mic:
        level, muted, name = 0.12, False, "Микрофон (USB Audio)"

        def switch(self, spec):
            self.switched = spec
    app.mic, app.voice_active = Mic(), True
    assert c.get("/api/audio/level").json()["level"] == 0.12
    assert "слушает" in c.post("/api/audio/test", json={}).json()["error"]
    r = c.post("/api/audio/device", json={"spec": "Микрофон (Realtek)|MME"}).json()
    assert r["applied"] and app.mic.switched == "Микрофон (Realtek)|MME"
    app.voice_active = False
