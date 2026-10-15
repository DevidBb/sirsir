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


def check_packages(voice: bool) -> list[CheckResult]:
    results = [_module("numpy", "обработка звука")]
    if voice:
        results += [
            _module("sounddevice", "микрофон"),
            _module("vosk", "фраза пробуждения"),
            _module("faster_whisper", "распознавание речи"),
            _module("pyttsx3", "синтез речи"),
        ]
    results += [
        _module("psutil", "системная информация", required=False),
        _module("send2trash", "удаление в корзину", required=False),
    ]
    if sys.platform == "win32":
        results.append(_module("pygetwindow", "управление окнами", required=False))
    return results


def check_ollama(config: Config) -> list[CheckResult]:
    client = OllamaClient(config.get("llm.base_url"), config.get("llm.model"))
    try:
        models = client.list_models()
    except LLMError as e:
        return [CheckResult("Ollama", False, f"{e}. Запустите Ollama (ollama serve)")]
    res = [CheckResult("Ollama", True, f"доступна, моделей: {len(models)}")]
    ok = client.has_model()
    res.append(CheckResult(f"модель {client.model}", ok,
                           "установлена" if ok else f"не найдена; выполните: ollama pull {client.model}"))
    return res


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
        import ctranslate2  # type: ignore
        n = ctranslate2.get_cuda_device_count()
        return CheckResult("GPU NVIDIA", n > 0, f"CUDA-устройств: {n}" if n else
                           "CUDA недоступна для ctranslate2 — будет CPU", required=False)
    except ImportError:
        return CheckResult("GPU NVIDIA", True, "драйвер есть (ctranslate2 не проверен)", required=False)


def check_microphone(config: Config) -> CheckResult:
    try:
        import sounddevice as sd  # type: ignore
        dev = config.get("audio.input_device") or None
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


def run_all(config: Config, voice: bool = True) -> list[CheckResult]:
    results = [check_python(), *check_packages(voice), *check_ollama(config)]
    if voice:
        results += [check_vosk_model(config), check_whisper_model(config), check_gpu(),
                    check_microphone(config), check_tts(config)]
    return results


def report(results: list[CheckResult]) -> bool:
    for r in results:
        print(r.line())
    failed = [r for r in results if r.required and not r.ok]
    print("\nВсё готово." if not failed else f"\nНе пройдено обязательных проверок: {len(failed)}.")
    return not failed
