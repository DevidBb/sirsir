"""Значок в области уведомлений: состояние помощника и быстрые действия.

Голосовой режим (run.bat --tray): синий — ждёт «Джарвис», зелёный — слушает команду,
оранжевый — говорит, серый — микрофон выключен.
Режим HUD (run_web.bat --tray, автозапуск): значок-глаз как в HUD — красный — готов,
зелёный — слушает, янтарный — думает или выполняет, светлый — говорит, серый — микрофон
выключен; щелчок открывает HUD.
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
HUD_COLORS = {
    "idle": (255, 46, 59),
    "listening": (73, 214, 155),
    "thinking": (255, 181, 71),
    "acting": (255, 181, 71),
    "speaking": (255, 138, 114),
    "muted": (130, 130, 130),
    "error": (120, 18, 26),
}
HUD_TITLES = {
    "idle": "JARVIS — готов",
    "listening": "JARVIS — слушаю",
    "thinking": "JARVIS — думаю",
    "acting": "JARVIS — выполняю",
    "speaking": "JARVIS — говорю",
    "muted": "JARVIS — микрофон выключен",
    "error": "JARVIS — ошибка",
}


def make_icon_image(state: str, size: int = 64):
    from PIL import Image, ImageDraw  # type: ignore
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = COLORS.get(state, COLORS["idle"])
    d.ellipse((4, 4, size - 4, size - 4), fill=color + (255,))
    d.ellipse((size * 0.3, size * 0.3, size * 0.7, size * 0.7), fill=(255, 255, 255, 230))
    return img


def make_eye_image(state: str, size: int = 64):
    """Значок-глаз HUD: тёмный диск с кольцом и ядром цвета состояния."""
    from PIL import Image, ImageDraw  # type: ignore
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = HUD_COLORS.get(state, HUD_COLORS["idle"])
    d.ellipse((2, 2, size - 2, size - 2), fill=(20, 10, 11, 255), outline=color + (255,),
              width=max(2, size // 14))
    c, r = size / 2, size * 0.25
    d.ellipse((c - r, c - r, c + r, c + r), fill=color + (255,))
    h = size * 0.07
    d.ellipse((c - r * 0.45 - h, c - r * 0.45 - h, c - r * 0.45 + h, c - r * 0.45 + h),
              fill=(255, 255, 255, 220))
    return img


class Tray:
    def __init__(self, toggle_mic: Callable[[], None], is_muted: Callable[[], bool],
                 emergency_stop: Callable[[], None], quit_app: Callable[[], None], *,
                 open_ui: Callable[[], None] | None = None,
                 mic_available: Callable[[], bool] = lambda: True, look: str = "voice"):
        """look="hud" — значок-глаз и состояния HUD; open_ui — пункт «Открыть HUD» (по щелчку)."""
        self.toggle_mic = toggle_mic
        self.is_muted = is_muted
        self.emergency_stop = emergency_stop
        self.quit_app = quit_app
        self.open_ui = open_ui
        self.mic_available = mic_available
        self.hud = look == "hud"
        self._icon = None
        self.state = "idle"

    def image(self, state: str):
        return make_eye_image(state) if self.hud else make_icon_image(state)

    def title(self, state: str) -> str:
        return (HUD_TITLES if self.hud else TITLES).get(state, "JARVIS")

    def start(self) -> bool:
        try:
            import pystray  # type: ignore
        except ImportError:
            log.warning("pystray не установлен — значок в трее отключён")
            return False
        items = []
        if self.open_ui is not None:
            items.append(pystray.MenuItem("Открыть HUD", lambda: self.open_ui(), default=True))
        items += [
            pystray.MenuItem("Микрофон включён", lambda: self.toggle_mic(),
                             checked=lambda item: not self.is_muted(), default=self.open_ui is None,
                             visible=lambda item: self.mic_available()),
            pystray.MenuItem("Остановить (стоп)", lambda: self.emergency_stop()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", lambda: self._quit()),
        ]
        try:
            self._icon = pystray.Icon("jarvis", self.image("idle"), self.title("idle"), pystray.Menu(*items))
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
            self._icon.icon = self.image(state)
            self._icon.title = self.title(state)
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


def follow_hud(tray: Tray, is_muted: Callable[[], bool]) -> Callable[[dict], None]:
    """Подписчик событий HUD: цвет значка — состояние, напоминания — уведомлением Windows."""
    def on_event(event: dict) -> None:
        kind = event.get("type")
        if kind == "state":
            state = event.get("state", "idle")
            tray.set_state("muted" if state == "idle" and is_muted() else state)
        elif kind == "voice" and tray.state in ("idle", "muted"):
            tray.set_state("muted" if event.get("muted") else "idle")
        elif kind == "notice" and event.get("reminder"):
            tray.notify(str(event.get("text", "")))
    return on_event
