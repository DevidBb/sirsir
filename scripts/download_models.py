"""Скачивание локальных моделей речи (единственный шаг, которому нужен интернет).

- Vosk small ru 0.22 — ~45 МБ архив (~90 МБ на диске): фраза пробуждения, CPU.
- faster-whisper small — ~470 МБ: распознавание речи, GPU (≈1 ГБ VRAM) или CPU.

LLM qwen3:8b ставится отдельно: `ollama pull qwen3:8b` (~5.2 ГБ, ≈6 ГБ VRAM).
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.config import Config  # noqa: E402

VOSK_URL = "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip"


def download_vosk(target: Path) -> None:
    if target.is_dir():
        print(f"Vosk уже есть: {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "vosk.zip"
        print(f"Скачиваю {VOSK_URL} (~45 МБ)…")
        urllib.request.urlretrieve(VOSK_URL, archive)
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():  # защита от путей вида ../
                if member.startswith("/") or ".." in Path(member).parts:
                    raise RuntimeError(f"Подозрительный путь в архиве: {member}")
            zf.extractall(tmp)
        extracted = Path(tmp) / "vosk-model-small-ru-0.22"
        shutil.move(str(extracted), str(target))
    print(f"Vosk готов: {target}")


def download_whisper(name: str, models_dir: Path) -> None:
    from faster_whisper.utils import download_model
    models_dir.mkdir(parents=True, exist_ok=True)
    print(f"Скачиваю faster-whisper {name} (~470 МБ для small)…")
    path = download_model(name, cache_dir=str(models_dir))
    print(f"Whisper готов: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("-y", "--yes", action="store_true", help="не спрашивать подтверждение")
    args = parser.parse_args()
    config = Config.load()
    print(__doc__)
    if not args.yes and input("Скачать модели? [y/N] ").strip().lower() not in ("y", "yes", "д", "да"):
        print("Отменено.")
        return 1
    download_vosk(config.path("wake_word.vosk_model_path"))
    download_whisper(config.get("speech_recognition.model", "small"),
                     config.path("speech_recognition.models_dir"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
