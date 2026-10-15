"""Значок в области уведомлений: состояние помощника и быстрые действия.

Цвет значка: синий — ждёт «Джарвис», зелёный — слушает команду,
оранжевый — говорит, серый — микрофон выключен.
"""

from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger(__name__)

COLORS = {
    "idle": (40, 120, 220),
    "listening": (40, 180, 90),
    "speaking": (240, 150, 30),
    "muted": (130, 130, 130),
}
TITLES = {
    "idle": "JARVIS — жду «Джарвис»",
    "listening": "JARVIS — слушаю",
    "speaking": "JARVIS — говорю",
    "muted": "JARVIS — микрофон выключен",
}


def make_icon_image(state: str, size: int = 64):
    from PIL import Image, ImageDraw  # type: ignore
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = COLORS.get(state, COLORS["idle"])
    d.ellipse((4, 4, size - 4, size - 4), fill=color + (255,))
    d.ellipse((size * 0.3, size * 0.3, size * 0.7, size * 0.7), fill=(255, 255, 255, 230))
    return img


class Tray:
    def __init__(self, toggle_mic: Callable[[], None], is_muted: Callable[[], bool],
                 emergency_stop: Callable[[], None], quit_app: Callable[[], None]):
        self.toggle_mic = toggle_mic
        self.is_muted = is_muted
        self.emergency_stop = emergency_stop
        self.quit_app = quit_app
        self._icon = None
        self.state = "idle"

    def start(self) -> bool:
        try:
            import pystray  # type: ignore
        except ImportError:
            log.warning("pystray не установлен — значок в трее отключён")
            return False
        menu = pystray.Menu(
            pystray.MenuItem("Микрофон включён", lambda: self.toggle_mic(),
                             checked=lambda item: not self.is_muted(), default=True),
            pystray.MenuItem("Остановить (стоп)", lambda: self.emergency_stop()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", lambda: self._quit()),
        )
        try:
            self._icon = pystray.Icon("jarvis", make_icon_image("idle"), TITLES["idle"], menu)
            self._icon.run_detached()
            return True
        except Exception as e:
            log.warning("Не удалось показать значок в трее: %s", e)
            self._icon = None
            return False

    def _quit(self) -> None:
        self.quit_app()
        self.stop()

    def set_state(self, state: str) -> None:
        if state == self.state or self._icon is None:
            self.state = state
            return
        self.state = state
        try:
            self._icon.icon = make_icon_image(state)
            self._icon.title = TITLES.get(state, "JARVIS")
        except Exception as e:
            log.debug("Трей: %s", e)

    def notify(self, text: str) -> None:
        if self._icon is not None:
            try:
                self._icon.notify(text, "JARVIS")
            except Exception as e:
                log.debug("Уведомление не показано: %s", e)

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None
