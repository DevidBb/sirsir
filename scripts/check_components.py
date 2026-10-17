"""Проверка всех компонентов JARVIS.

    python scripts/check_components.py          — зависимости, модели, устройства
    python scripts/check_components.py --live   — плюс реальные пробы: ответ LLM,
                                                  фраза TTS, 4 секунды записи и распознавание
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis import console_utf8  # noqa: E402

console_utf8()

from jarvis import checks  # noqa: E402
from jarvis.config import Config  # noqa: E402


def live_llm(config: Config) -> bool:
    from jarvis.llm import LLMError, OllamaClient
    client = OllamaClient(config.get("llm.base_url"), config.get("llm.model"), num_predict=40)
    t = time.monotonic()
    try:
        msg = client.chat([{"role": "user", "content": "Скажи одно короткое приветствие по-русски."}])
    except LLMError as e:
        print(f"[FAIL] LLM: {e}")
        return False
    print(f"[OK  ] LLM ответила за {time.monotonic() - t:.1f} с: {msg.get('content')}")
    return True


def live_tts(config: Config) -> bool:
    from jarvis.main import make_speaker
    try:
        s = make_speaker(config)
        s.load()
        s.say("Проверка синтеза речи. Сейчас 12:30, загрузка 42%.")
        print(f"[OK  ] TTS ({s.engine_name}), голос: {s.voice_name}")
        return True
    except Exception as e:
        print(f"[FAIL] TTS: {e}")
        return False


def live_asr(config: Config) -> bool:
    import numpy as np
    import sounddevice as sd
    from jarvis.speech_recognition import SpeechRecognizer
    try:
        asr = SpeechRecognizer.from_config(config)
        device = asr.load()
        sr = int(config.get("audio.sample_rate", 16000))
        print("Говорите 4 секунды…")
        rec = sd.rec(int(4 * sr), samplerate=sr, channels=1, dtype="int16")
        sd.wait()
        audio = rec[:, 0].astype(np.float32) / 32768.0
        t = time.monotonic()
        text = asr.transcribe(audio)
        print(f"[OK  ] Распознавание ({device}, {time.monotonic() - t:.1f} с): «{text}»")
        return True
    except Exception as e:
        print(f"[FAIL] Распознавание: {e}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="реальные пробы LLM, TTS и микрофона")
    parser.add_argument("--text", action="store_true", help="без голосовых компонентов")
    args = parser.parse_args()
    config = Config.load()
    ok = checks.report(checks.run_all(config, voice=not args.text))
    if args.live and ok:
        print("\nЖивые проверки:")
        ok = live_llm(config) and ok
        if not args.text:
            ok = live_tts(config) and ok
            ok = live_asr(config) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
