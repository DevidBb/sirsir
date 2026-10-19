"""Проверка зависимостей, моделей и устройств перед запуском."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from dataclasses import dataclass

from .config import Config
from .llm import LLMError, OllamaClient


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    required: bool = True

    def line(self) -> str:
        mark = "OK  " if self.ok else ("FAIL" if self.required else "WARN")
        return f"[{mark}] {self.name}: {self.detail}"


def _module(name: str, purpose: str, required: bool = True) -> CheckResult:
    found = importlib.util.find_spec(name) is not None
    return CheckResult(f"пакет {name}", found,
                       purpose if found else f"не установлен ({purpose}); pip install -r requirements.txt",
                       required)


def check_python() -> CheckResult:
    ok = sys.version_info >= (3, 11)
    return CheckResult("Python", ok, sys.version.split()[0] + ("" if ok else " — нужен 3.11+"))


def silero_ready(config: Config) -> bool:
    from .text_to_speech import resolve_silero_model
    return (resolve_silero_model(config.path("tts.silero_model_path")).is_file()
            and importlib.util.find_spec("torch") is not None)


def uses_system_voice(config: Config) -> bool:
    engine = config.get("tts.engine", "auto")
    return engine == "pyttsx3" or (engine in ("auto", "silero") and not silero_ready(config))


def check_packages(voice: bool, config: Config | None = None) -> list[CheckResult]:
    results = [_module("numpy", "обработка звука")]
    if voice:
        results += [
            _module("sounddevice", "микрофон"),
            _module("vosk", "фраза пробуждения"),
            _module("faster_whisper", "распознавание речи"),
        ]
        # Системный голос нужен, только если не будет Silero.
        results.append(_module("pyttsx3", "синтез речи",
                               required=config is None or uses_system_voice(config)))
    results += [
        _module("psutil", "системная информация", required=False),
        _module("send2trash", "удаление в корзину", required=False),
    ]
    if sys.platform == "win32":
        results.append(_module("pygetwindow", "управление окнами", required=False))
    return results


def check_ollama(config: Config) -> list[CheckResult]:
    # Ollama обязательна, только если выбрана как провайдер модели.
    # Гибрид использует Ollama для быстрых обычных вопросов и fallback.
    required = str(config.get("llm.provider", "ollama")).lower() in ("ollama", "hybrid")
    client = OllamaClient(config.get("llm.base_url"), config.get("llm.model"))
    try:
        models = client.list_models()
    except LLMError as e:
        return [CheckResult("Ollama", False, f"{e}. Запустите Ollama (ollama serve)", required)]
    res = [CheckResult("Ollama", True, f"доступна, моделей: {len(models)}", required)]
    ok = client.has_model()
    res.append(CheckResult(f"модель {client.model}", ok,
                           "установлена" if ok else f"не найдена; выполните: ollama pull {client.model}",
                           required))
    return res


def check_claude(config: Config) -> list[CheckResult]:
    if str(config.get("llm.provider", "ollama")) != "claude":
        return []
    from .providers import ClaudeAgentBackend
    ok, note = ClaudeAgentBackend.available()
    return [CheckResult("Claude Agent SDK", ok, note)]


def check_gemini(config: Config) -> list[CheckResult]:
    if str(config.get("llm.provider", "ollama")) != "gemini":
        return []
    from .gemini import KEY_ENV, api_key
    ok = bool(api_key())
    return [CheckResult("Gemini", ok, "ключ найден" if ok else
                        f"нет ключа; добавьте {KEY_ENV}=... в .env (https://aistudio.google.com/apikey)")]


def check_browser(config: Config) -> list[CheckResult]:
    """Playwright и его Chromium: нужны для поиска и управления браузером (не обязательны)."""
    if not config.get("browser.enabled", True):
        return []
    if importlib.util.find_spec("playwright") is None:
        return [CheckResult("браузер (Playwright)", False,
                            "не установлен; pip install -r requirements.txt", required=False)]
    channel = str(config.get("browser.channel", "chromium") or "")
    if channel in ("msedge", "chrome"):
        return [CheckResult("браузер (Playwright)", True, f"используется установленный {channel}",
                            required=False)]
    import os
    from pathlib import Path
    roots = [config.root / ".playwright", Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "") or "~"),
             Path.home() / ".cache" / "ms-playwright",
             Path(os.environ.get("LOCALAPPDATA", "~")) / "ms-playwright"]
    found = any(r.expanduser().is_dir() and any(r.expanduser().glob("chromium-*")) for r in roots)
    return [CheckResult("браузер (Playwright)", found,
                        "Chromium для Playwright найден" if found else
                        "Chromium не скачан; выполните: python -m playwright install chromium",
                        required=False)]


def check_vosk_model(config: Config) -> CheckResult:
    path = config.path("wake_word.vosk_model_path")
    ok = path.is_dir() and (path / "am").is_dir()
    return CheckResult("модель Vosk", ok,
                       str(path) if ok else f"нет в {path}; python scripts/download_models.py")


def check_whisper_model(config: Config) -> CheckResult:
    root = config.path("speech_recognition.models_dir")
    name = config.get("speech_recognition.model", "small")
    found = root.is_dir() and any(name in p.name for p in root.iterdir())
    return CheckResult(f"модель Whisper {name}", found,
                       str(root) if found else f"нет в {root}; python scripts/download_models.py")


def check_gpu() -> CheckResult:
    if shutil.which("nvidia-smi") is None:
        return CheckResult("GPU NVIDIA", False, "nvidia-smi не найден — Whisper будет работать на CPU",
                           required=False)
    try:
        from .speech_recognition import add_nvidia_dll_dirs
        add_nvidia_dll_dirs()
        import ctranslate2  # type: ignore
        n = ctranslate2.get_cuda_device_count()
        return CheckResult("GPU NVIDIA", n > 0, f"CUDA-устройств: {n}" if n else
                           "CUDA недоступна для ctranslate2 — будет CPU", required=False)
    except ImportError:
        return CheckResult("GPU NVIDIA", True, "драйвер есть (ctranslate2 не проверен)", required=False)


def check_microphone(config: Config) -> CheckResult:
    try:
        import sounddevice as sd  # type: ignore
        from .audio_input import resolve_device
        dev = resolve_device(sd, config.get("audio.input_device") or None)
        info = sd.query_devices(dev, "input")
        return CheckResult("микрофон", True, info["name"])
    except Exception as e:
        return CheckResult("микрофон", False, f"не найден: {e}")


def check_tts(config: Config) -> CheckResult:
    try:
        import pyttsx3  # type: ignore
        engine = pyttsx3.init()
        voices = engine.getProperty("voices") or []
        ru = [v.name for v in voices
              if any(k in f"{v.id} {v.name}".lower() for k in ("ru", "russian", "irina", "pavel"))]
        if ru:
            return CheckResult("русский голос TTS", True, ru[0])
        return CheckResult("русский голос TTS", False,
                           "нет; Параметры → Время и язык → Речь → добавить русский голос",
                           required=False)
    except Exception as e:
        return CheckResult("TTS", False, f"ошибка инициализации: {e}")


def check_silero(config: Config) -> list[CheckResult]:
    engine = config.get("tts.engine")
    if engine == "auto":
        return [CheckResult("голос", True, "Silero" if silero_ready(config) else
                            "системный (для Silero: install.bat или README)", required=False)]
    if engine != "silero":
        return []
    from .text_to_speech import resolve_silero_model
    path = resolve_silero_model(config.path("tts.silero_model_path"))
    torch_ok = importlib.util.find_spec("torch") is not None
    return [
        CheckResult("пакет torch", torch_ok, "для голоса Silero" if torch_ok else
                    "не установлен; pip install -r requirements-silero.txt (иначе — системный голос)",
                    required=False),
        CheckResult("модель Silero", path.is_file(), str(path) if path.is_file() else
                    f"нет в {path}; python scripts/download_models.py --only-silero", required=False),
    ]


def run_all(config: Config, voice: bool = True) -> list[CheckResult]:
    results = [check_python(), *check_packages(voice, config), *check_ollama(config),
               *check_claude(config), *check_gemini(config),
               *check_browser(config)]
    if voice:
        results += [check_vosk_model(config), check_whisper_model(config), check_gpu(),
                    check_microphone(config)]
        if uses_system_voice(config):
            results.append(check_tts(config))
        results += check_silero(config)
    return results


def report(results: list[CheckResult]) -> bool:
    for r in results:
        print(r.line())
    failed = [r for r in results if r.required and not r.ok]
    print("\nВсё готово." if not failed else f"\nНе пройдено обязательных проверок: {len(failed)}.")
    return not failed
