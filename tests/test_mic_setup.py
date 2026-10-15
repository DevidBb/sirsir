import numpy as np

from jarvis.mic_setup import analyze_levels, input_devices, meter

SR = 16000
rng = np.random.default_rng(0)


def tone(amp, sec=2.0):
    t = np.arange(int(SR * sec)) / SR
    return (amp * np.sin(2 * np.pi * 200 * t) * 32767).astype(np.int16)


def noise(amp, sec=2.0):
    return (rng.normal(0, amp, int(SR * sec)) * 32767).astype(np.int16)


def test_good_recording():
    rep = analyze_levels(noise(0.002), tone(0.15))
    assert rep.verdict == "хорошо" and rep.advice == [] and rep.snr > 20


def test_too_quiet():
    rep = analyze_levels(noise(0.002), tone(0.005))
    assert rep.verdict == "плохо" and any("не слышна" in a for a in rep.advice)


def test_noisy_room():
    rep = analyze_levels(noise(0.04), tone(0.08))
    assert any("шум" in a for a in rep.advice) and any("громче шума" in a for a in rep.advice)


def test_clipping():
    t = np.arange(SR * 2) / SR
    loud = np.clip(1.5 * np.sin(2 * np.pi * 200 * t), -1, 1)  # микрофон упирается в предел
    rep = analyze_levels(noise(0.002), (loud * 32767).astype(np.int16))
    assert any("перегруз" in a for a in rep.advice)


def test_meter():
    assert meter(0) == "·" * 30 and meter(1.0) == "█" * 30


def test_input_devices_filters_outputs():
    class SD:
        @staticmethod
        def query_hostapis():
            return [{"name": "MME"}, {"name": "Windows WASAPI"}]

        @staticmethod
        def query_devices():
            return [{"name": "Микрофон", "max_input_channels": 1, "hostapi": 0, "default_samplerate": 44100.0},
                    {"name": "Динамики", "max_input_channels": 0, "hostapi": 0, "default_samplerate": 48000.0},
                    {"name": "Микрофон", "max_input_channels": 2, "hostapi": 1, "default_samplerate": 48000.0}]
    assert input_devices(SD) == [(0, "Микрофон", "MME", 44100), (2, "Микрофон", "Windows WASAPI", 48000)]


def test_virtual_microphones_marked():
    from jarvis.mic_setup import is_virtual
    assert is_virtual("Microphone (Steam Streaming Microphone)")
    assert is_virtual("CABLE Output (VB-Audio Virtual Cable)")
    assert not is_virtual("Микрофон (Realtek(R) Audio)")
    assert not is_virtual("Microphone (2- USB Audio Device)")


def test_saved_microphone_visible_to_startup_check(config):
    """Выбор из мастера (memory.db) должен перекрывать config.toml ещё до проверок."""
    from jarvis.main import apply_saved_settings
    from jarvis.memory import Memory
    mem = Memory(config.path("memory.db_path"))
    mem.set_setting("audio.input_device", "Микрофон (Realtek(R) Audio)|MME")
    mem.close()
    assert not config.get("audio.input_device")
    apply_saved_settings(config)
    assert config.get("audio.input_device") == "Микрофон (Realtek(R) Audio)|MME"
