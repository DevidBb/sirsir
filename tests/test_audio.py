import numpy as np

from jarvis.audio_input import EndpointDetector, VadConfig, record_command, rms
from jarvis.wake_word import strip_wake_phrase

SR = 16000
BLOCK = SR * 30 // 1000
rng = np.random.default_rng(0)


def noise(n_blocks, amp=0.002):
    return [(rng.normal(0, amp, BLOCK) * 32768).astype(np.int16) for _ in range(n_blocks)]


def speech(n_blocks, amp=0.1):
    t = np.arange(BLOCK) / SR
    return [(amp * np.sin(2 * np.pi * 220 * t) * 32768).astype(np.int16) for _ in range(n_blocks)]


def cfg(**kw):
    base = dict(sample_rate=SR, block_ms=30, silence_ms_to_stop=600, max_command_seconds=10,
                no_speech_timeout_seconds=3, energy_ratio=3.0, min_speech_ms=150)
    base.update(kw)
    return VadConfig(**base)


def test_rms_int16():
    assert abs(rms(np.full(100, 16384, np.int16)) - 0.5) < 1e-3


def test_records_phrase_until_silence():
    blocks = noise(20) + speech(40) + noise(30) + speech(100)
    audio = record_command(iter(blocks), cfg())
    assert audio is not None and audio.dtype == np.float32
    # ~40 блоков речи + преролл + ~600 мс тишины, но не вторая фраза
    seconds = len(audio) / SR
    assert 1.2 < seconds < 2.5


def test_silence_times_out():
    assert record_command(iter(noise(200)), cfg()) is None


def test_short_click_is_not_speech():
    blocks = noise(10) + speech(2) + noise(150)
    assert record_command(iter(blocks), cfg()) is None


def test_max_length_cuts_long_speech():
    d = EndpointDetector(cfg(max_command_seconds=1))
    states = [d.feed(b) for b in speech(50)]
    assert "end" in states
    assert states.index("end") <= 34  # 1 секунда = ~33 блока по 30 мс


def test_strip_wake_phrase():
    phrases = ["джарвис"]
    assert strip_wake_phrase("Джарвис, открой блокнот", phrases) == "открой блокнот"
    assert strip_wake_phrase("открой блокнот", phrases) == "открой блокнот"
    assert strip_wake_phrase("Джарвис", phrases) == ""
