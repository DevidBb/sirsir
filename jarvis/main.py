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
from typing import Any, Callable

from . import calculator, checks, console_utf8, files, mcp_client, site_studio, skills, team, vault
from .assistant import EXIT, MUTE, STOP, Assistant, Reply
from .config import Config, load_dotenv
from .gemini import GEMINI_HOST, api_key as gemini_key, make_llm
from .memory import Memory
from .notes import Notes
from .providers import make_agent_backend
from .reminders import Reminders
from .schedules import Scheduler
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
        # HUD: наблюдатель голосовых событий и общий замок — голос и чат не обрабатываются одновременно.
        self.observer: Callable[[dict], None] = lambda event: None
        self.handle_lock = threading.RLock()
        # «Слушать» из HUD: принять команду без фразы пробуждения.
        self.force_listen = threading.Event()
        self.voice_active = False
        self.memory = Memory(config.path("memory.db_path"), int(config.get("memory.max_facts", 200)))
        self._apply_saved_settings()

        # Ollama или Gemini — для цикла инструментов (Claude работает отдельно, см. providers.py).
        self.llm = make_llm(config)
        self.speaker = make_speaker(config, voice)

        self.registry = ToolRegistry()
        self.controller = WindowsController(config)
        self.controller.register_tools(self.registry)
        self.browser = None
        self.web_tools = None
        if config.get("browser.enabled", True):
            self._setup_browser()
        self.memory.register_tools(self.registry, on_setting_changed=self._on_setting_changed)
        self.reminders = Reminders(self.memory)
        self.reminders.register_tools(self.registry)
        self.notes = Notes(self.memory)
        self.notes.register_tools(self.registry)
        # Заметки Obsidian ([memory.vault]): проекты, решения, проверки, следующие шаги — в файлах;
        # факты и итоги задач — зеркалом из memory.db. Правила обмена — в начале vault.py.
        self.vault = vault.setup(config, self.memory)
        if self.vault is not None:
            self.vault.register_tools(self.registry)
        self.schedules = Scheduler(self.memory)
        self.schedules.register_tools(self.registry)
        calculator.register_tools(self.registry)
        self._register_voice_tool()
        self._register_screen_tool()
        # Файлы и проекты только в [files] allowed_dirs; изменения — после diff и подтверждения.
        self.files = files.setup(config, self.registry)
        # Внешние MCP-серверы ([mcp.servers.*]): подключаются в фоне, инструменты появляются по готовности.
        self.mcp = mcp_client.setup(config, self.registry)
        self.assistant = Assistant(config, self.llm, self.registry, self.memory,
                                   on_setting_changed=self._on_setting_changed)
        self.assistant.agent_backend = make_agent_backend(config)
        self.assistant.vault = self.vault
        # Навыки: skills/<имя>/SKILL.md — инструкция и набор инструментов для повторяемых задач.
        self.skills = skills.setup(config, self.registry)
        self.assistant.skills = self.skills
        # Студия сайтов: «сделай сайт для …» — разбор старого сайта, тексты моделью, вёрстка, проверка.
        self.sites = site_studio.setup(config, self.assistant, self.browser,
                                       self.web_tools.search if self.web_tools else None)
        self.sites.register_tools(self.registry)
        self.assistant.sites = self.sites
        # Команда ИИ: «спроси всех: …» — Claude, ChatGPT (Codex), Gemini и локальная модель отвечают
        # параллельно, ведущий сводит ответы в один итог.
        self.team = team.setup(config, self.assistant)
        self.assistant.team = self.team
        self.sites.team = self.team  # «сделай сайт … командой»: тексты проверяют остальные ИИ

    def _setup_browser(self) -> None:
        """Браузер Джарвиса (Playwright). Без Playwright помощник работает как раньше."""
        from importlib.util import find_spec
        if find_spec("playwright") is None:
            log.warning("Playwright не установлен: поиск и управление браузером недоступны")
            return
        from .browser import BrowserController
        from .web_search import WebSearch
        from .web_tools import WebTools
        local_browsers = self.config.root / ".playwright"
        self.browser = BrowserController(
            self.config.path("browser.profile_dir"),
            headless=bool(self.config.get("browser.headless", False)),
            channel=str(self.config.get("browser.channel", "chromium") or ""),
            timeout_ms=int(self.config.get("browser.timeout_ms", 20000)),
            browsers_path=local_browsers if local_browsers.exists() else None,
            screenshots_dir=self.config.root / "data" / "screenshots")
        self.web_tools = WebTools(self.browser, WebSearch(
            self.browser, self.config.get("browser.search_engines", ["duckduckgo", "bing"])))
        self.web_tools.register_tools(self.registry)

    def set_provider(self, provider: str) -> str:
        """Переключение модели из интерфейса (сохраняется в memory.db)."""
        from .providers import PROVIDERS
        if provider not in PROVIDERS:
            raise ValueError(f"Неизвестный провайдер: {provider}")
        previous = str(self.config.get("llm.provider", "ollama"))
        self.config.set("llm.provider", provider)
        self.memory.set_setting("llm.provider", provider)
        transports = {"gemini": "gemini", "ollama_cloud": "ollama_cloud"}
        if transports.get(provider, "local") != transports.get(previous, "local"):
            self.llm.unload()
            self.llm = self.assistant.llm = make_llm(self.config)
        if self.assistant.agent_backend is not None:
            self.assistant.agent_backend.close()
        self.assistant.agent_backend = make_agent_backend(self.config)
        if self.assistant.agent_backend is not None:
            self.assistant.agent_backend.warm_up(self.assistant)
        return provider

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

    def _register_screen_tool(self) -> None:
        from .screen import ScreenError, capture_screen
        from .tool_registry import Tool, ToolResult

        def screenshot() -> ToolResult:
            try:
                path = capture_screen(self.config.root / "data" / "screenshots")
            except (ScreenError, OSError) as e:
                return ToolResult(f"Не удалось сделать снимок экрана: {e}", ok=False)
            return ToolResult(f"Снимок экрана сохранён: data/screenshots/{path.name}",
                              {"kind": "screen", "file": path.name})
        # Модель может сохранить снимок, но не видит его: показывать экран модели можно
        # только по прямой просьбе пользователя («посмотри на экран»).
        self.registry.register(Tool("screen_screenshot", "Сохранить снимок всего экрана в файл "
                                    "(сам снимок тебе не передаётся)", screenshot, category="system"))

    def _register_voice_tool(self) -> None:
        from .text_to_speech import SILERO_VOICES
        from .tool_registry import Param, Tool
        order = list(SILERO_VOICES)

        def set_voice(voice: str) -> str:
            current = self.speaker.silero_speaker if self.speaker.silero_speaker in order else order[0]
            if voice == "next":
                voice = order[(order.index(current) + 1) % len(order)]
            elif voice in ("male", "female"):
                kind = "мужской" if voice == "male" else "женский"
                same = [v for v in order if SILERO_VOICES[v][1] == kind]
                # Уже такой — берём следующий того же пола.
                voice = same[(same.index(current) + 1) % len(same)] if current in same else same[0]
            if not self.speaker.set_voice(voice):
                return ("Сейчас работает системный голос Windows. Чтобы выбирать голоса, "
                        "установите Silero: install.bat.")
            self.memory.set_setting("tts.silero_speaker", voice)
            self.config.set("tts.silero_speaker", voice)
            return f"Теперь я говорю так. Это голос {SILERO_VOICES[voice][0].capitalize()}."

        self.registry.register(Tool(
            "set_voice", "Сменить голос помощника (Silero): aidar, eugene — мужские; "
            "baya, kseniya, xenia — женские; next — следующий; male/female — мужской/женский",
            set_voice, {"voice": Param("string", "Голос", enum=order + ["next", "male", "female"])}))

    def _preload_llm(self) -> None:
        if self.config.get("llm.preload", True):
            threading.Thread(target=self.llm.preload, daemon=True, name="llm-preload").start()
            if self.assistant.agent_backend is not None:  # Claude: запустить Claude Code заранее
                self.assistant.agent_backend.warm_up(self.assistant)

    # --- напоминания ---
    def _on_reminder(self, text: str) -> None:
        self.observer({"type": "notice", "text": text, "reminder": True})
        if self.voice:
            self.notifications.put(text)
            self.wake_interrupt.set()
            if self.tray:
                self.tray.notify(text)
        else:
            self.speaker.say(text)

    def start_schedules(self, on_due) -> None:
        """Запустить планировщик (один раз: в HUD — через очередь интерфейса)."""
        if self.config.get("schedules.enabled", True):
            self.schedules.start(on_due)

    def _run_scheduled(self, schedule) -> None:
        """Голосовой и текстовый режимы: выполнить в потоке планировщика и сообщить итог."""
        with self.handle_lock:
            text = self.schedules.execute(schedule, self.assistant)
        short = text if len(text) <= 300 else text[:300].rsplit(" ", 1)[0] + "…"
        self._on_reminder(f"Плановая задача «{schedule.prompt}»: {short}")

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
        self.start_schedules(self._run_scheduled)
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
        asr = SpeechRecognizer.from_config(cfg)
        print(f"Распознавание речи: {asr.load()}")
        self.speaker.load()
        print(f"Голос: {self.speaker.describe()}")
        mic = Microphone(sr, vad.block_ms, cfg.get("audio.input_device") or None)
        mic.start()
        print(f"Микрофон: {mic.name}")
        return VoiceParts(mic, wake, stop_detector, asr, vad, phrases)

    def run_voice(self, parts: VoiceParts | None = None, console: bool = True) -> None:
        parts = parts or self.build_voice()
        mic = parts.mic
        self.mic = mic
        self.voice_active = True
        self.stop_detector = parts.stop_detector
        if console:
            self._start_console()
        if self.use_tray:
            from .tray import Tray
            self.tray = Tray(self.toggle_mic, lambda: mic.muted, self.emergency_stop, self.quit)
            self.tray.start()
        self.reminders.start(self._on_reminder)
        self.start_schedules(self._run_scheduled)
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
            self.voice_active = False
            self.observer({"type": "voice_state", "state": "off"})
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
        if self.stop_event.is_set():
            # «Выход» пришёл раньше (например, пока звучало напоминание): quit() ставит stop_event
            # до wake_interrupt, и сигнал прерывания мы только что стёрли — без этой проверки
            # цикл ждал бы «Джарвис» вместо выхода.
            return False
        if self.force_listen.is_set() or wake.wait(mic.blocks(self.wake_interrupt)):
            heard_wake = not self.force_listen.is_set()
        else:
            if self.stop_event.is_set():
                return False
            if mic.error:
                print(f"{mic.error}. Помощник остановлен.")
                self.observer({"type": "notice", "text": f"Микрофон: {mic.error}. Голос остановлен."})
                return False
            wake.reset()
            if not self.force_listen.is_set():
                return True  # разбудило напоминание
            heard_wake = False
        self.force_listen.clear()
        # Короткий сигнал не сбрасывает очередь микрофона: если команда
        # сказана сразу после «Джарвис», она попадёт в запись целиком.
        print("(слышу «Джарвис» — говорите команду)" if heard_wake else "(слушаю — говорите команду)",
              flush=True)
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
        self.observer({"type": "voice_state", "state": state})

    def listen_now(self) -> None:
        """Кнопка «Слушать» в HUD: следующая фраза — команда, без «Джарвис»."""
        if self.mic is not None and self.mic.muted:
            self.mic.unmute()
        self.force_listen.set()
        self.wake_interrupt.set()

    def set_mic(self, on: bool) -> None:
        if self.mic is None:
            return
        if on and self.mic.muted:
            self.toggle_mic()
        elif not on and not self.mic.muted:
            self.toggle_mic()
        self.wake_interrupt.set()

    def voice_status(self) -> dict[str, Any]:
        return {"enabled": self.voice_active, "muted": bool(self.mic and self.mic.muted),
                "mic": getattr(self.mic, "name", None) if self.mic else None,
                "error": getattr(self, "voice_error", "")}

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
            first_turn = first
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
                elif first_turn:
                    print("(команду не услышал — после сигнала скажите её громче или ближе к микрофону)",
                          flush=True)
                return
            heard = asr.transcribe(audio)
            text = strip_wake_phrase(heard, phrases)
            if not text:
                if first_turn and not heard:
                    print("(речь не разобрал)", flush=True)
                continue
            print(f"Вы: {text}", flush=True)
            self.observer({"type": "voice_heard", "text": text})
            self.barged_in = False

            def speak_sentence(sentence: str) -> bool:
                # Озвучиваем ответ по предложениям, пока модель дописывает остальное.
                if self.barged_in:
                    return False
                self._speak(mic, sentence)
                return not self.barged_in

            with self.handle_lock:
                reply = self.assistant.handle(text, on_sentence=speak_sentence)
            self.observer({"type": "voice_reply", "reply": reply})
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
        if self.assistant.agent_backend is not None:
            self.assistant.agent_backend.close(wait=5)
        self.team.close()
        self.reminders.stop()
        self.schedules.stop()
        if self.mcp is not None:
            self.mcp.close()
        if self.browser is not None:
            self.browser.close()
        if self.vault is not None:
            self.vault.close()  # отложенная синхронизация — до закрытия базы
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


def voices_demo(config: Config, choose=input) -> int:
    """Прослушать голоса Silero и сохранить выбранный (voices.bat)."""
    from .text_to_speech import SILERO_VOICES, Speaker
    speaker = Speaker("silero", rate=int(config.get("tts.rate", 185)),
                      silero_model=config.path("tts.silero_model_path"))
    speaker.load()
    if speaker.engine_name != "silero":
        print("Silero не установлен — работает системный голос. Установите его через install.bat.")
        return 1
    names = list(SILERO_VOICES)
    for n, voice in enumerate(names, 1):
        title, kind = SILERO_VOICES[voice]
        print(f"{n}. {title.capitalize()} ({kind})")
        speaker.set_voice(voice)
        speaker.say(f"Здравствуйте! Я Джарвис, и это голос {title.capitalize()}. Чем могу помочь?")
    answer = choose("\nНомер голоса, который оставить (Enter — не менять): ").strip()
    if answer.isdigit() and 1 <= int(answer) <= len(names):
        voice = names[int(answer) - 1]
        memory = Memory(config.path("memory.db_path"))
        memory.set_setting("tts.silero_speaker", voice)
        memory.close()
        print(f"Сохранено: {SILERO_VOICES[voice][0].capitalize()}. Запускайте run.bat.")
    return 0


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
    parser.add_argument("--web", action="store_true",
                        help="интерфейс HUD в браузере (http://127.0.0.1:8765)")
    parser.add_argument("--no-open", action="store_true", help="с --web: не открывать страницу HUD")
    parser.add_argument("--voice", action="store_true",
                        help="с --web: ещё и локальный голос (Vosk, Whisper, Silero) с показом в HUD")
    parser.add_argument("--check", action="store_true", help="только проверить компоненты")
    parser.add_argument("--skip-check", action="store_true", help="не проверять при запуске")
    parser.add_argument("--tray", action="store_true", help="значок в области уведомлений")
    parser.add_argument("--setup-mic", action="store_true",
                        help="выбрать и проверить микрофон")
    parser.add_argument("--report", action="store_true",
                        help="собрать диагностический отчёт в data/report.txt")
    parser.add_argument("--autostart", choices=["on", "off", "status"],
                        help="автозапуск при входе в Windows")
    parser.add_argument("--voices", action="store_true",
                        help="прослушать все голоса Silero и выбрать понравившийся")
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

    try:
        config = Config.load(args.config)
    except (OSError, ValueError) as e:
        print(f"Не удалось прочитать настройки: {e}")
        return 1
    load_dotenv(config.root / ".env")
    # Настройки, сохранённые голосом и мастером микрофона (memory.db), — до проверок,
    # иначе проверка показывает микрофон по умолчанию, а не выбранный.
    apply_saved_settings(config)
    voice = not args.text and (not args.web or args.voice)
    setup_logging(config, args.verbose)
    setup_audit_log(config)
    if args.setup_mic:
        from . import mic_setup
        return mic_setup.run(config)
    if args.voices:
        return voices_demo(config)
    if args.report:
        from . import report
        path = report.write(config)
        print(f"Отчёт сохранён: {path}\nПроверьте содержимое и пришлите файл разработчику.")
        return 0
    import platform as _platform
    log.info("Запуск JARVIS: %s, Python %s, режим %s", _platform.platform(),
             sys.version.split()[0], "голос" if voice else "текст")

    provider = str(config.get("llm.provider", "ollama"))
    needs_local = provider in ("ollama", "hybrid") or (provider == "team" and "local" in config.get("team.members", []))
    if config.get("llm.autostart_ollama", True) and needs_local:
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
        if not ok and args.web:
            print("Часть компонентов недоступна — подробности на панели «Диагностика» в HUD.")
        elif not ok:
            print("Исправьте ошибки выше или запустите с --skip-check.")
            return 1

    hosts = list(config.get("security.allowed_hosts", ["127.0.0.1", "localhost"]))
    if gemini_key():  # Gemini — HTTPS из процесса помощника, только к API Google
        hosts.append(GEMINI_HOST)
    from .ollama_cloud import HOST as OLLAMA_CLOUD_HOST, api_key as cloud_key
    if cloud_key():
        hosts.append(OLLAMA_CLOUD_HOST)
    NetworkGuard(hosts).install()
    if args.web:
        from . import web_app

        def build() -> App:
            core = App(config, voice=args.voice)
            if core.browser is not None:  # «открой ютуб» — в браузере Джарвиса, а не в системном
                core.controller.open_url = core.browser.open_from_other_thread
            return core
        return web_app.run(config, build, open_browser=False if args.no_open else None,
                           voice=args.voice, tray=args.tray)
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
