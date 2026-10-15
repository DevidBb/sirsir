"""Точка входа: сборка компонентов и главный голосовой цикл.

    python -m jarvis            — голосовой режим
    python -m jarvis --text     — текстовый режим (без микрофона и моделей речи)
    python -m jarvis --check    — только проверка компонентов
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from typing import Any

from . import checks
from .assistant import EXIT, MUTE, STOP, Assistant, Reply
from .config import Config
from .llm import OllamaClient
from .memory import Memory
from .security import NetworkGuard
from .text_to_speech import Speaker
from .tool_registry import ToolRegistry
from .windows_control import WindowsController

log = logging.getLogger("jarvis")


class App:
    def __init__(self, config: Config, voice: bool):
        self.config = config
        self.voice = voice
        self.stop_event = threading.Event()
        self.memory = Memory(config.path("memory.db_path"), int(config.get("memory.max_facts", 200)))
        self._apply_saved_settings()

        self.llm = OllamaClient(
            config.get("llm.base_url"), config.get("llm.model"),
            temperature=float(config.get("llm.temperature", 0.4)),
            num_predict=int(config.get("llm.num_predict", 220)),
            num_ctx=int(config.get("llm.num_ctx", 4096)),
            timeout=float(config.get("llm.timeout_seconds", 90)),
            keep_alive=str(config.get("llm.keep_alive", "5m")))
        self.speaker = Speaker(config.get("tts.engine", "pyttsx3") if voice else "print",
                               rate=int(config.get("tts.rate", 185)),
                               volume=float(config.get("tts.volume", 1.0)),
                               voice_hint=config.get("tts.voice_hint", "ru"))

        self.registry = ToolRegistry()
        WindowsController(config).register_tools(self.registry)
        self.memory.register_tools(self.registry, on_setting_changed=self._on_setting_changed)
        self.assistant = Assistant(config, self.llm, self.registry, self.memory,
                                   on_setting_changed=self._on_setting_changed)

    # --- настройки ---
    def _apply_saved_settings(self) -> None:
        for key, value in self.memory.all_settings().items():
            self.config.set(key, value)

    def _on_setting_changed(self, key: str, value: Any) -> None:
        self.config.set(key, value)
        if key == "tts.rate":
            self.speaker.rate = int(value)
        elif key == "tts.volume":
            self.speaker.volume = float(value)
        elif key == "llm.num_predict":
            self.llm.num_predict = int(value)
        self.speaker.apply_settings()

    # --- общий обработчик ---
    def respond(self, text: str) -> Reply | None:
        reply = self.assistant.handle(text)
        if reply is None:
            return None
        if reply.action == STOP:
            self.speaker.interrupt()
        self.speaker.say(reply.text)
        if reply.action == EXIT:
            self.stop_event.set()
        return reply

    # --- текстовый режим ---
    def run_text(self) -> None:
        print("Текстовый режим. Пишите команды; «заверши работу» или Ctrl+C — выход.")
        while not self.stop_event.is_set():
            try:
                line = input("> ")
            except (EOFError, KeyboardInterrupt):
                break
            self.respond(line)

    # --- голосовой режим ---
    def run_voice(self) -> None:
        from .audio_input import Microphone, VadConfig, record_command
        from .speech_recognition import SpeechRecognizer
        from .wake_word import VoskWakeWord, strip_wake_phrase

        cfg = self.config
        sr = int(cfg.get("audio.sample_rate", 16000))
        vad = VadConfig(sample_rate=sr, block_ms=int(cfg.get("audio.block_ms", 30)),
                        silence_ms_to_stop=int(cfg.get("audio.silence_ms_to_stop", 900)),
                        max_command_seconds=float(cfg.get("audio.max_command_seconds", 15)),
                        no_speech_timeout_seconds=float(cfg.get("audio.no_speech_timeout_seconds", 6)),
                        energy_ratio=float(cfg.get("audio.vad_energy_ratio", 3.0)),
                        min_speech_ms=int(cfg.get("audio.min_speech_ms", 250)))
        phrases = cfg.get("wake_word.phrases", ["джарвис"])

        wake = VoskWakeWord(cfg.path("wake_word.vosk_model_path"), phrases, sr)
        wake.load()
        asr = SpeechRecognizer(cfg.get("speech_recognition.model", "small"),
                               cfg.get("speech_recognition.device", "cuda"),
                               cfg.get("speech_recognition.compute_type_gpu", "float16"),
                               cfg.get("speech_recognition.compute_type_cpu", "int8"),
                               cfg.path("speech_recognition.models_dir"),
                               cfg.get("assistant.language", "ru"),
                               int(cfg.get("speech_recognition.beam_size", 1)))
        print(f"Распознавание речи: {asr.load()}")
        self.speaker.load()

        mic = Microphone(sr, vad.block_ms, cfg.get("audio.input_device") or None)
        mic.start()
        self._start_console(mic)
        self.speaker.say(f"{cfg.get('assistant.name', 'Джарвис')} готов.")
        print("Скажите «Джарвис». Enter — вкл/выкл микрофон, s+Enter — стоп, q+Enter — выход.")

        try:
            while not self.stop_event.is_set():
                if mic.muted:
                    self.stop_event.wait(0.3)
                    continue
                if not wake.wait(mic.blocks(self.stop_event)):
                    if mic.error:
                        print(f"{mic.error}. Помощник остановлен.")
                    break
                self._speak(mic, "Слушаю.")
                self._dialog(mic, vad, asr, phrases, record_command, strip_wake_phrase)
                wake.reset()
        finally:
            mic.stop()

    def _speak(self, mic, text: str) -> None:
        # Микрофон выключен на время речи, чтобы помощник не слышал сам себя.
        was_muted = mic.muted
        mic.mute()
        self.speaker.say(text)
        if not was_muted:
            mic.unmute()

    def _dialog(self, mic, vad, asr, phrases, record_command, strip_wake_phrase) -> None:
        first = True
        while not self.stop_event.is_set() and not mic.muted:
            if first:
                cfg = vad
            else:
                follow = float(self.config.get("assistant.follow_up_seconds", 8))
                if follow <= 0:
                    return
                cfg = type(vad)(**{**vad.__dict__, "no_speech_timeout_seconds": follow})
            first = False
            audio = record_command(mic.blocks(self.stop_event), cfg)
            if audio is None:
                return
            text = strip_wake_phrase(asr.transcribe(audio), phrases)
            if not text:
                continue
            print(f"Вы: {text}", flush=True)
            reply = self.assistant.handle(text)
            if reply is None:
                continue
            if reply.action == STOP:
                self.speaker.interrupt()
            self._speak(mic, reply.text)
            if reply.action == MUTE:
                mic.mute()
                return
            if reply.action == EXIT:
                self.stop_event.set()
                return
            if not reply.expect_followup:
                return

    def _start_console(self, mic) -> None:
        def loop() -> None:
            while not self.stop_event.is_set():
                try:
                    cmd = input().strip().lower()
                except (EOFError, KeyboardInterrupt):
                    return
                if cmd == "q":
                    self.stop_event.set()
                    self.speaker.interrupt()
                elif cmd == "s":
                    self.speaker.interrupt()
                    self.assistant.handle(self.assistant.emergency.phrases[0])
                    print("Аварийная остановка.")
                else:
                    if mic.muted:
                        mic.unmute()
                        print("Микрофон включён.")
                    else:
                        mic.mute()
                        print("Микрофон выключен.")
        threading.Thread(target=loop, daemon=True, name="console").start()

    def close(self) -> None:
        self.memory.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="Локальный голосовой помощник")
    parser.add_argument("--config", help="путь к config.toml")
    parser.add_argument("--text", action="store_true", help="текстовый режим без микрофона")
    parser.add_argument("--check", action="store_true", help="только проверить компоненты")
    parser.add_argument("--skip-check", action="store_true", help="не проверять при запуске")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    # Модели только локальные: запрещаем библиотекам ходить в интернет.
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    config = Config.load(args.config)
    voice = not args.text

    if args.check or not args.skip_check:
        ok = checks.report(checks.run_all(config, voice=voice))
        if args.check:
            return 0 if ok else 1
        if not ok:
            print("Исправьте ошибки выше или запустите с --skip-check.")
            return 1

    NetworkGuard(config.get("security.allowed_hosts", ["127.0.0.1", "localhost"])).install()
    app = App(config, voice)
    try:
        app.run_voice() if voice else app.run_text()
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
    print("JARVIS остановлен.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
