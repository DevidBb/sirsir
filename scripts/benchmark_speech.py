"""Проверка речевой цепочки на синтезированной речи — без микрофона.

Голосом Silero произносятся фразы, которые прогоняются через настоящие Vosk
(«Джарвис», «стоп») и Whisper. Показывает точность и скорость на этом ПК.
Нужны: голос Silero (requirements-silero.txt, download_models.py --silero),
модели Vosk и Whisper.

    python scripts/benchmark_speech.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis import console_utf8  # noqa: E402

console_utf8()

import numpy as np  # noqa: E402

from jarvis.config import Config  # noqa: E402
from jarvis.intents import IntentMatcher  # noqa: E402
from jarvis.speech_recognition import SpeechRecognizer  # noqa: E402
from jarvis.text_to_speech import SileroBackend  # noqa: E402
from jarvis.wake_word import VoskWakeWord, strip_wake_phrase  # noqa: E402

SPEAKERS = ["aidar", "baya", "eugene", "kseniya", "xenia"]
WAKE_POS = ["Джарвис.", "Джарвис, открой блокнот.", "Эй, Джарвис, который час?",
            "Слушай, Джарвис, сделай потише."]
WAKE_NEG = ["Завтра будет дождь.", "Мне нравится эта песня.", "Где мои ключи?",
            "Позвони мне вечером.", "Сколько стоит билет?", "Гарвардский университет.",
            "Варвара пошла в магазин.", "Мы поедем в Париж.", "Чарльз Дарвин написал книгу.",
            "Кошка спит на диване."]
OWN_ANSWERS = ["Сейчас двенадцать сорок три, пятнадцать октября, четверг.", "Запускаю блокнот.",
               "Таймер на пять минут запущен.",
               "Чёрная дыра — это область пространства, из которой не может выйти даже свет.",
               "Требуется подтверждение: переместить в корзину файл отчёт.", "Хорошо, отменяю."]
STOPS = ["Стоп.", "Хватит.", "Замолчи."]
COMMANDS = ["Джарвис, открой блокнот.", "Поставь таймер на пять минут.", "Который час?",
            "Напомни через двадцать минут выключить плиту.", "Сделай погромче.",
            "Что такое чёрная дыра?", "Подтверждаю."]
# «Открой …» — что должно открыться. На них видно, помогает ли подсказка Whisper (hotwords).
OPEN_COMMANDS = [("Открой ютуб.", "ютуб"), ("Джарвис, открой ютуб.", "ютуб"), ("Открой гитхаб.", "гитхаб"),
                 ("Открой википедию.", "википедия"), ("Открой почту.", "почта"),
                 ("Открой блокнот.", "блокнот"), ("Открой диспетчер задач.", "диспетчер задач")]

rng = np.random.default_rng(0)


def main() -> int:
    cfg = Config.load()
    tts = SileroBackend(cfg.path("tts.silero_model_path"))
    try:
        tts.load()
    except Exception as e:
        print(f"Нужен голос Silero: {e}")
        return 1
    tts.apply(185, 1.0)

    def say16(text: str, speaker: str) -> np.ndarray:
        tts.speaker = speaker
        a = tts.synthesize(text)
        n = len(a) // 3
        a = a[: n * 3].reshape(n, 3).mean(axis=1)  # 48 → 16 кГц
        return np.concatenate([rng.normal(0, .003, 8000), a, rng.normal(0, .003, 8000)]).astype(np.float32)

    def heard(det: VoskWakeWord, audio: np.ndarray) -> bool:
        det.reset()
        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
        return det.wait(pcm[i:i + 480] for i in range(0, len(pcm) - 480, 480))

    wake = VoskWakeWord(cfg.path("wake_word.vosk_model_path"), cfg.get("wake_word.phrases"))
    wake.load()
    stop = wake.with_phrases(cfg.get("assistant.interrupt_phrases"))

    print("== «Джарвис» (5 голосов) ==")
    hits = sum(heard(wake, say16(t, s)) for s in SPEAKERS for t in WAKE_POS)
    false = [(t, s) for s in SPEAKERS for t in WAKE_NEG if heard(wake, say16(t, s))]
    print(f"  срабатываний: {hits}/{len(SPEAKERS) * len(WAKE_POS)}")
    print(f"  ложных: {len(false)}/{len(SPEAKERS) * len(WAKE_NEG)} {false}")

    print("== Перебивание ==")
    own = [t for t in OWN_ANSWERS if heard(stop, say16(t, cfg.get("tts.silero_speaker", "aidar")))]
    stops = sum(heard(stop, say16(t, s)) for s in SPEAKERS[1:] for t in STOPS)
    print(f"  ложных от собственного голоса: {len(own)}/{len(OWN_ANSWERS)} {own}")
    print(f"  «стоп» пользователя: {stops}/{len(SPEAKERS[1:]) * len(STOPS)}")

    print("== Распознавание ==")
    asr = SpeechRecognizer.from_config(cfg)
    print(f"  устройство: {asr.load()}")
    asr.transcribe(say16("Проверка.", "baya"))  # прогрев
    times = []
    for phrase in COMMANDS:
        t = time.monotonic()
        text = asr.transcribe(say16(phrase, "eugene"))
        times.append(time.monotonic() - t)
        print(f"  {times[-1]:4.1f} с  {phrase} → {strip_wake_phrase(text, cfg.get('wake_word.phrases'))}")
    print(f"  среднее время: {sum(times) / len(times):.2f} с")
    noise = rng.normal(0, 0.02, 48000).astype(np.float32)
    print(f"  на шуме: {asr.transcribe(noise)!r} (должно быть пусто)")

    print("== Команды «открой …»: подсказка Whisper ==")
    matcher = IntentMatcher(cfg.get("windows.apps", {}), cfg.get("windows.websites", {}),
                            cfg.get("windows.folders", {}))
    audio = [(want, say16(phrase, s)) for phrase, want in OPEN_COMMANDS for s in SPEAKERS]
    for label, hot in (("без подсказки", "Джарвис"), (f"hotwords «{asr.hotwords}»", None)):
        r = SpeechRecognizer.from_config(cfg, hotwords=hot)
        r._model, r.device = asr._model, asr.device  # та же модель, другая подсказка
        ok, times, wrong = 0, [], []
        for want, a in audio:
            t = time.monotonic()
            text = r.transcribe(a)
            times.append(time.monotonic() - t)
            intent = matcher.match(text)
            if intent and intent.args.get("name") == want:
                ok += 1
            else:
                wrong.append(text)
        print(f"  {label}: {ok}/{len(audio)}, {sum(times) / len(times):.2f} с на фразу")
        if wrong:
            print(f"    не поняты: {wrong}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
