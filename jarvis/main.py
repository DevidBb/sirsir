"""Точка входа: сборка компонентов и главный голосовой цикл.

    python -m jarvis            — голосовой режим
    python -m jarvis --text     — текстовый режим (без микрофона и моделей речи)
    python -m jarvis --check    — только проверка компонентов
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

from . import calculator, checks, console_utf8
from .assistant import EXIT, MUTE, STOP, Assistant, Reply
from .config import Config
from .llm import OllamaClient
from .memory import Memory
from .notes import Notes
from .reminders import Reminders
from .security import NetworkGuard
from .text_to_speech import Speaker, play_beep
from .tool_registry import ToolRegistry
from .windows_control import WindowsController

log = logging.getLogger("jarvis")


@dataclass
class VoiceParts:
    mic: Any
    wake: Any
    stop_detector: Any
    asr: Any
    vad: Any
    phrases: list[str]


def make_speaker(config: Config, voice: bool = True) -> Speaker:
    return Speaker(config.get("tts.engine", "auto") if voice else "print",
                   rate=int(config.get("tts.rate", 185)),
                   volume=float(config.get("tts.volume", 1.0)),
                   voice_hint=config.get("tts.voice_hint", "ru"),
                   silero_model=config.path("tts.silero_model_path"),
                   silero_speaker=config.get("tts.silero_speaker", "aidar"))


class App:
    def __init__(self, config: Config, voice: bool, tray: bool = False):
        self.config = config
        self.voice = voice
        self.use_tray = tray
        self.tray = None
        self.mic = None
        self.barged_in = False
        self.stop_event = threading.Event()
        # Будит голосовой цикл, когда нужно что-то сказать без «Джарвис».
        self.wake_interrupt = threading.Event()
        self.notifications: queue.Queue[str] = queue.Queue()
        self.memory = Memory(config.path("memory.db_path"), int(config.get("memory.max_facts", 200)))
        self._apply_saved_settings()

        self.llm = OllamaClient(
            config.get("llm.base_url"), config.get("llm.model"),
            temperature=float(config.get("llm.temperature", 0.4)),
            num_predict=int(config.get("llm.num_predict", 220)),
            num_ctx=int(config.get("llm.num_ctx", 8192)),
            timeout=float(config.get("llm.timeout_seconds", 90)),
            keep_alive=str(config.get("llm.keep_alive", "30m")))
        self.speaker = make_speaker(config, voice)

        self.registry = ToolRegistry()
        WindowsController(config).register_tools(self.registry)
        self.memory.register_tools(self.registry, on_setting_changed=self._on_setting_changed)
        self.reminders = Reminders(self.memory)
        self.reminders.register_tools(self.registry)
        self.notes = Notes(self.memory)
        self.notes.register_tools(self.registry)
        calculator.register_tools(self.registry)
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

    def _preload_llm(self) -> None:
        if self.config.get("llm.preload", True):
            threading.Thread(target=self.llm.preload, daemon=True, name="llm-preload").start()

    # --- напоминания ---
    def _on_reminder(self, text: str) -> None:
        if self.voice:
            self.notifications.put(text)
            self.wake_interrupt.set()
            if self.tray:
                self.tray.notify(text)
        else:
            self.speaker.say(text)

    def _deliver_notifications(self) -> None:
        while True:
            try:
                text = self.notifications.get_nowait()
            except queue.Empty:
                return
            play_beep()
            self._speak(self.mic, text)

    # --- общий обработчик ---
    def respond(self, text: str) -> Reply | None:
        reply = self.assistant.handle(text, on_sentence=lambda s: self.speaker.say(s) or True)
        if reply is None:
            return None
        if reply.action == STOP:
            self.speaker.interrupt()
        if not reply.spoken:
            self.speaker.say(reply.text)
        if reply.action == EXIT:
            self.stop_event.set()
        return reply

    # --- текстовый режим ---
    def run_text(self) -> None:
        print("Текстовый режим. Пишите команды; «заверши работу» или Ctrl+C — выход.")
        self.reminders.start(self._on_reminder)
        self._preload_llm()
        while not self.stop_event.is_set():
            try:
                line = input("> ")
            except (EOFError, KeyboardInterrupt):
                break
            self.respond(line)

    # --- голосовой режим ---
    def build_voice(self) -> VoiceParts:
        """Загружает модели и открывает микрофон."""
        from .audio_input import Microphone, VadConfig
        from .speech_recognition import SpeechRecognizer
        from .wake_word import VoskWakeWord

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
        # Детектор «стоп» на той же модели — слушает, пока помощник говорит.
        stop_detector = (wake.with_phrases(cfg.get("assistant.interrupt_phrases", ["стоп"]))
                         if cfg.get("assistant.barge_in", True) else None)
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
        print(f"Микрофон: {mic.name}")
        return VoiceParts(mic, wake, stop_detector, asr, vad, phrases)

    def run_voice(self, parts: VoiceParts | None = None, console: bool = True) -> None:
        parts = parts or self.build_voice()
        mic = parts.mic
        self.mic = mic
        self.stop_detector = parts.stop_detector
        if console:
            self._start_console()
        if self.use_tray:
            from .tray import Tray
            self.tray = Tray(self.toggle_mic, lambda: mic.muted, self.emergency_stop, self.quit)
            self.tray.start()
        self.reminders.start(self._on_reminder)
        self._preload_llm()
        self.speaker.say(f"{self.config.get('assistant.name', 'Джарвис')} готов.")
        print("Скажите «Джарвис». Enter — вкл/выкл микрофон, s+Enter — стоп, q+Enter — выход.")

        errors: list[float] = []
        try:
            while not self.stop_event.is_set():
                try:
                    if not self._voice_iteration(parts):
                        break
                except Exception:
                    # Одна ошибка (драйвер, модель, инструмент) не должна ронять помощника,
                    # но частые ошибки подряд — повод остановиться, а не крутиться впустую.
                    log.exception("Ошибка в голосовом цикле")
                    now = time.monotonic()
                    errors = [t for t in errors if now - t < 60] + [now]
                    if len(errors) >= 5:
                        print("Слишком много ошибок подряд, помощник остановлен. Подробности в логе.")
                        break
                    self.speaker.say("Произошла ошибка, продолжаю работу.")
        finally:
            mic.stop()
            if self.tray:
                self.tray.stop()

    def _voice_iteration(self, parts: VoiceParts) -> bool:
        """Одно ожидание «Джарвис» и диалог. False — пора завершаться."""
        mic, wake = parts.mic, parts.wake
        if not self.notifications.empty():
            self._deliver_notifications()
        if mic.muted:
            self._state("muted")
            self.wake_interrupt.wait(0.3)
            self.wake_interrupt.clear()
            return True
        self._state("idle")
        self.wake_interrupt.clear()
        if not wake.wait(mic.blocks(self.wake_interrupt)):
            if self.stop_event.is_set():
                return False
            if mic.error:
                print(f"{mic.error}. Помощник остановлен.")
                return False
            wake.reset()
            return True  # разбудило напоминание
        # Короткий сигнал не сбрасывает очередь микрофона: если команда
        # сказана сразу после «Джарвис», она попадёт в запись целиком.
        if self.config.get("assistant.wake_feedback", "beep") == "voice":
            self._speak(mic, "Слушаю.")
        else:
            play_beep()
        try:
            self._dialog(parts)
        finally:
            wake.reset()
        return True

    # --- управление из консоли и трея ---
    def _state(self, state: str) -> None:
        if self.tray:
            self.tray.set_state(state)

    def toggle_mic(self) -> None:
        if self.mic is None:
            return
        if self.mic.muted:
            self.mic.unmute()
            print("Микрофон включён.", flush=True)
        else:
            self.mic.mute()
            print("Микрофон выключен.", flush=True)

    def emergency_stop(self) -> None:
        # Из консоли/трея — другой поток: только флаги, без правки истории диалога.
        self.assistant.emergency_stop()
        self.barged_in = True
        self.speaker.interrupt()
        print("Аварийная остановка.", flush=True)

    def quit(self) -> None:
        self.stop_event.set()
        self.wake_interrupt.set()
        self.speaker.interrupt()

    def _speak(self, mic, text: str) -> None:
        self._state("speaking")
        detector = getattr(self, "stop_detector", None)
        if detector is None or mic.muted:
            # Микрофон выключен на время речи, чтобы помощник не слышал сам себя.
            was_muted = mic.muted
            mic.mute()
            self.speaker.say(text)
            if not was_muted:
                mic.unmute()
            return

        # Перебивание: пока идёт речь, слушаем только фразы «стоп/хватит».
        done = threading.Event()

        def monitor() -> None:
            detector.reset()
            for block in mic.blocks(done, timeout=0.1):
                if detector.process(block):
                    print("(перебито)", flush=True)
                    self.barged_in = True
                    self.speaker.interrupt()
                    return

        watcher = threading.Thread(target=monitor, daemon=True, name="barge-in")
        watcher.start()
        try:
            self.speaker.say(text)
        finally:
            done.set()
            watcher.join(timeout=1)
            mic.flush()  # отбрасываем эхо собственной речи

    def _dialog(self, parts: VoiceParts) -> None:
        from .audio_input import record_command
        from .wake_word import strip_wake_phrase
        mic, vad, asr, phrases = parts.mic, parts.vad, parts.asr, parts.phrases
        first = True
        while not self.stop_event.is_set() and not mic.muted:
            if first:
                cfg = vad
            else:
                follow = float(self.config.get("assistant.follow_up_seconds", 8))
                if self.assistant.dictation.active:
                    follow = float(self.config.get("assistant.dictation_pause_seconds", 30))
                if follow <= 0:
                    return
                cfg = type(vad)(**{**vad.__dict__, "no_speech_timeout_seconds": follow})
            first = False
            self._state("listening")
            audio = record_command(mic.blocks(self.stop_event), cfg)
            if audio is None:
                if self.assistant.dictation.active:
                    self.assistant.dictation.active = False
                    self._speak(mic, "Диктовка завершена из-за паузы.")
                return
            text = strip_wake_phrase(asr.transcribe(audio), phrases)
            if not text:
                continue
            print(f"Вы: {text}", flush=True)
            self.barged_in = False

            def speak_sentence(sentence: str) -> bool:
                # Озвучиваем ответ по предложениям, пока модель дописывает остальное.
                if self.barged_in:
                    return False
                self._speak(mic, sentence)
                return not self.barged_in

            reply = self.assistant.handle(text, on_sentence=speak_sentence)
            if reply is None:
                continue
            if reply.action == STOP:
                self.speaker.interrupt()
            if not reply.spoken:
                self._speak(mic, reply.text)
            if reply.action == MUTE:
                mic.mute()
                return
            if reply.action == EXIT:
                self.quit()
                return
            if not reply.expect_followup:
                return

    def _start_console(self) -> None:
        def loop() -> None:
            while not self.stop_event.is_set():
                try:
                    cmd = input().strip().lower()
                except (EOFError, KeyboardInterrupt, RuntimeError):
                    return
                if cmd == "q":
                    self.quit()
                elif cmd == "s":
                    self.emergency_stop()
                else:
                    self.toggle_mic()
        threading.Thread(target=loop, daemon=True, name="console").start()

    def close(self) -> None:
        self.reminders.stop()
        self.memory.close()


def setup_logging(config: Config, verbose: bool) -> None:
    """Консоль — только важное (INFO с -v); файл — всегда подробно, для диагностики."""
    from logging.handlers import RotatingFileHandler
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO if verbose else logging.WARNING)
    console.setFormatter(fmt)
    root.addHandler(console)
    path = config.path("assistant.log_file")
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(fmt)
    root.addHandler(handler)


def setup_audit_log(config: Config) -> None:
    """Журнал действий: что и с какими аргументами выполнялось (без записи звука)."""
    from logging.handlers import RotatingFileHandler
    path = config.path("security.audit_log")
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    audit = logging.getLogger("jarvis.audit")
    audit.setLevel(logging.INFO)
    audit.propagate = False
    audit.addHandler(handler)


def apply_saved_settings(config: Config) -> None:
    """Перекрыть config.toml настройками из memory.db (выбранный микрофон и т.п.)."""
    try:
        memory = Memory(config.path("memory.db_path"))
    except Exception as e:  # повреждённая база не должна мешать запуску
        log.warning("Не удалось прочитать сохранённые настройки: %s", e)
        return
    try:
        for key, value in memory.all_settings().items():
            config.set(key, value)
    finally:
        memory.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="Локальный голосовой помощник")
    parser.add_argument("--config", help="путь к config.toml")
    parser.add_argument("--text", action="store_true", help="текстовый режим без микрофона")
    parser.add_argument("--check", action="store_true", help="только проверить компоненты")
    parser.add_argument("--skip-check", action="store_true", help="не проверять при запуске")
    parser.add_argument("--tray", action="store_true", help="значок в области уведомлений")
    parser.add_argument("--setup-mic", action="store_true",
                        help="выбрать и проверить микрофон")
    parser.add_argument("--report", action="store_true",
                        help="собрать диагностический отчёт в data/report.txt")
    parser.add_argument("--autostart", choices=["on", "off", "status"],
                        help="автозапуск при входе в Windows")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    console_utf8()
    # Модели только локальные: запрещаем библиотекам ходить в интернет.
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    if args.autostart:
        from . import autostart
        try:
            if args.autostart == "on":
                print(f"Автозапуск включён: {autostart.enable()}")
            elif args.autostart == "off":
                print("Автозапуск выключен." if autostart.disable() else "Автозапуск и так выключен.")
            else:
                print("Автозапуск включён." if autostart.is_enabled() else "Автозапуск выключен.")
        except OSError as e:
            print(e)
            return 1
        return 0

    config = Config.load(args.config)
    # Настройки, сохранённые голосом и мастером микрофона (memory.db), — до проверок,
    # иначе проверка показывает микрофон по умолчанию, а не выбранный.
    apply_saved_settings(config)
    voice = not args.text
    setup_logging(config, args.verbose)
    setup_audit_log(config)
    if args.setup_mic:
        from . import mic_setup
        return mic_setup.run(config)
    if args.report:
        from . import report
        path = report.write(config)
        print(f"Отчёт сохранён: {path}\nПроверьте содержимое и пришлите файл разработчику.")
        return 0
    import platform as _platform
    log.info("Запуск JARVIS: %s, Python %s, режим %s", _platform.platform(),
             sys.version.split()[0], "голос" if voice else "текст")

    if config.get("llm.autostart_ollama", True):
        from .llm import OllamaClient as _Client, ensure_ollama
        status = ensure_ollama(_Client(config.get("llm.base_url"), config.get("llm.model")))
        if status == "started":
            print("Ollama была не запущена — запустил её.")
        elif status == "failed":
            print("Ollama не удалось запустить автоматически.")

    if args.check or not args.skip_check:
        ok = checks.report(checks.run_all(config, voice=voice))
        if args.check:
            return 0 if ok else 1
        if not ok:
            print("Исправьте ошибки выше или запустите с --skip-check.")
            return 1

    NetworkGuard(config.get("security.allowed_hosts", ["127.0.0.1", "localhost"])).install()
    app = App(config, voice, tray=args.tray)
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
