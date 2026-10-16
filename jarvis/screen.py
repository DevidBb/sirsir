"""Снимок экрана и подготовка изображения для модели.

Снимок всего экрана делается только по прямой команде пользователя («посмотри на экран»,
«сделай скриншот»): у модели нет инструмента, который сам снимает экран, поэтому текст
с веб-страницы не может заставить Джарвиса отправить экран в облако.
"""

from __future__ import annotations

import base64
import io
import time
from pathlib import Path
from typing import Callable

# Длинная сторона для модели: больше — дороже и медленнее, а мелкий текст и так читается.
MAX_SIDE = 1568


class ScreenError(RuntimeError):
    pass


def _default_grab():
    try:
        from PIL import ImageGrab
    except ImportError as e:
        raise ScreenError("не установлен Pillow (pip install -r requirements.txt)") from e
    try:
        return ImageGrab.grab(all_screens=True)
    except Exception as e:  # noqa: BLE001 — нет дисплея, нет прав и т.п.
        raise ScreenError(f"не удалось снять экран: {e}") from e


def capture_screen(folder: str | Path, grab: Callable[[], object] | None = None) -> Path:
    image = (grab or _default_grab)()
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / time.strftime("screen-%Y%m%d-%H%M%S.png")
    image.save(path)
    return path


def image_for_model(path: str | Path, max_side: int = MAX_SIDE) -> tuple[str, str]:
    """(base64, media type): JPEG, уменьшенный до max_side по длинной стороне."""
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        scale = max_side / max(im.size)
        if scale < 1:
            im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"
