"""Мастер настройки микрофона: выбор устройства, уровни, пробное распознавание.

    python -m jarvis --setup-mic     (или setup_mic.bat)

Выбранный микрофон сохраняется в настройках (SQLite) по имени — индекс
устройства в Windows может меняться после перезагрузки.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .audio_input import resample, rms
from .config import Config

SR = 16000


@dataclass
class LevelReport:
    noise: float
    speech: float
    snr: float
    clipped: float
    verdict: str
    advice: list[str]


def analyze_levels(noise: np.ndarray, speech: np.ndarray, energy_ratio: float = 3.0) -> LevelReport:
    """Оценка записи: шум в тишине, громкость речи, перегруз."""
    n = rms(noise)
    # Громкость речи — по самым громким 30% блоков (паузы между словами не в счёт).
    blocks = [speech[i:i + 480] for i in range(0, len(speech) - 480, 480)] or [speech]
    levels = sorted(rms(b) for b in blocks)
    s = float(np.mean(levels[int(len(levels) * 0.7):])) if levels else 0.0
    snr = s / max(n, 1e-6)
    x = speech.astype(np.float32) / (32768.0 if speech.dtype == np.int16 else 1.0)
    clipped = float(np.mean(np.abs(x) > 0.98))
    advice: list[str] = []
    if s < 0.01:
        advice.append("Речь почти не слышна: проверьте, тот ли микрофон выбран, и поднимите "
                      "уровень в Параметрах Windows → Система → Звук → Ввод.")
    elif s < 0.03:
        advice.append("Речь тихая: говорите ближе к микрофону или поднимите уровень записи.")
    if clipped > 0.01:
        advice.append("Есть перегруз (хрипы): уменьшите уровень записи в Windows.")
    if n > 0.02:
        advice.append("Сильный фоновый шум: вентилятор, музыка? Распознавание будет хуже.")
    if snr < energy_ratio:
        advice.append(f"Речь громче шума всего в {snr:.1f} раза — конец фразы будет "
                      f"определяться плохо (нужно больше {energy_ratio:g}).")
    verdict = "хорошо" if not advice else ("плохо" if s < 0.01 or snr < energy_ratio else "терпимо")
    return LevelReport(n, s, snr, clipped, verdict, advice)


def meter(level: float, width: int = 30) -> str:
    filled = min(width, int(level / 0.3 * width))
    return "█" * filled + "·" * (width - filled)


def _record(sd, device, seconds: float) -> np.ndarray:
    """Запись с откатом на родную частоту устройства."""
    try:
        data = sd.rec(int(seconds * SR), samplerate=SR, channels=1, dtype="int16", device=device)
        sd.wait()
        return data[:, 0]
    except Exception:
        native = int(sd.query_devices(device, "input")["default_samplerate"])
        data = sd.rec(int(seconds * native), samplerate=native, channels=1, dtype="int16", device=device)
        sd.wait()
        return resample(data[:, 0], native, SR)


def input_devices(sd) -> list[tuple[int, str, str, int]]:
    hostapis = sd.query_hostapis()
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            out.append((i, d["name"], hostapis[d["hostapi"]]["name"], int(d["default_samplerate"])))
    return out


def run(config: Config) -> int:
    try:
        import sounddevice as sd  # type: ignore
    except ImportError:
        print("Нужен пакет sounddevice: pip install -r requirements.txt")
        return 1
    devices = input_devices(sd)
    if not devices:
        print("Микрофоны не найдены. Подключите микрофон и проверьте Параметры Windows → Звук.")
        return 1
    default = sd.default.device[0]
    print("Микрофоны:")
    for i, name, api, rate in devices:
        print(f"  {'*' if i == default else ' '}{i:3}: {name}  [{api}, {rate} Гц]")
    choice = input("\nНомер микрофона (Enter — отмеченный *): ").strip()
    device = int(choice) if choice.isdigit() else None
    chosen = next(((n, api) for i, n, api, _ in devices
                   if i == (device if device is not None else default)), ("", ""))
    name = chosen[0]

    input("\nШаг 1. Помолчите 3 секунды. Нажмите Enter и молчите…")
    noise = _record(sd, device, 3)
    print(f"  шум:   {meter(rms(noise))} {rms(noise):.4f}")
    input("\nШаг 2. Нажмите Enter и скажите обычным голосом: «Джарвис, который час?»")
    speech = _record(sd, device, 4)
    rep = analyze_levels(noise, speech, float(config.get("audio.vad_energy_ratio", 3.0)))
    print(f"  речь:  {meter(rep.speech)} {rep.speech:.4f}")
    print(f"\nРечь громче шума в {rep.snr:.1f} раза. Оценка: {rep.verdict}.")
    for a in rep.advice:
        print(f"  • {a}")

    _try_recognition(config, speech)

    if input(f"\nСохранить микрофон «{name}»? [Y/n] ").strip().lower() in ("", "y", "д", "да"):
        from .memory import Memory
        mem = Memory(config.path("memory.db_path"))
        # «имя|интерфейс»: индексы в Windows меняются, имена повторяются в MME/WASAPI.
        mem.set_setting("audio.input_device", f"{chosen[0]}|{chosen[1]}" if device is not None else "")
        mem.close()
        print("Сохранено. Запускайте помощника: run.bat")
    return 0


def _try_recognition(config: Config, speech: np.ndarray) -> None:
    """Если модели есть — проверить запись тем же, что использует помощник."""
    try:
        from .wake_word import VoskWakeWord
        wake = VoskWakeWord(config.path("wake_word.vosk_model_path"), config.get("wake_word.phrases"))
        wake.load()
        hit = wake.wait(speech[i:i + 480] for i in range(0, len(speech) - 480, 480))
        print(f"\n«Джарвис» {'услышан ✓' if hit else 'НЕ услышан ✗ — говорите чётче или громче'}")
    except Exception as e:
        print(f"\n(проверка «Джарвис» пропущена: {e})")
    try:
        from .speech_recognition import SpeechRecognizer
        asr = SpeechRecognizer(config.get("speech_recognition.model", "small"),
                               config.get("speech_recognition.device", "cuda"),
                               models_dir=config.path("speech_recognition.models_dir"))
        asr.load()
        text = asr.transcribe(speech.astype(np.float32) / 32768.0)
        print(f"Распознано: «{text}»" if text else "Распознано: (ничего)")
    except Exception as e:
        print(f"(проверка распознавания пропущена: {e})")
