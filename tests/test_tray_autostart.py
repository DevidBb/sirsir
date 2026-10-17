import builtins

import pytest

from jarvis import autostart
from jarvis.tray import COLORS, Tray


def test_autostart_enable_disable(tmp_path):
    folder = tmp_path / "Startup"
    assert not autostart.is_enabled(folder)
    path = autostart.enable(tmp_path / "Проект", folder)
    text = path.read_text(encoding="utf-8")
    assert "chcp 65001" in text and "Проект" in text and "run_web.bat --tray --no-open" in text
    assert b"\r\n" in path.read_bytes()
    assert autostart.is_enabled(folder)
    assert autostart.disable(folder)
    assert not autostart.disable(folder)


def test_startup_dir_requires_appdata(monkeypatch):
    monkeypatch.delenv("APPDATA", raising=False)
    with pytest.raises(OSError):
        autostart.startup_dir()


def test_icon_colors():
    pytest.importorskip("PIL")
    from jarvis.tray import make_icon_image
    for state, color in COLORS.items():
        img = make_icon_image(state)
        assert img.size == (64, 64)
        assert img.getpixel((8, 32))[:3] == color


def test_tray_without_pystray(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "pystray":
            raise ImportError
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    t = Tray(lambda: None, lambda: False, lambda: None, lambda: None)
    assert t.start() is False
    t.set_state("muted")  # без значка — просто не падает
    t.notify("x")
    assert t.state == "muted"


def test_eye_icon_colors():
    pytest.importorskip("PIL")
    from jarvis.tray import HUD_COLORS, make_eye_image
    for state, color in HUD_COLORS.items():
        img = make_eye_image(state)
        assert img.size == (64, 64)
        assert img.getpixel((40, 40))[:3] == color      # ядро
        assert img.getpixel((0, 0))[3] == 0              # углы прозрачные


class FakeIcon:
    def __init__(self, name, icon, title, menu):
        self.icon, self.title, self.menu = icon, title, menu
        self.notified = []
        self.stopped = False

    def run_detached(self):
        pass

    def notify(self, text, title):
        self.notified.append(text)

    def stop(self):
        self.stopped = True


class FakeMenuItem:
    def __init__(self, text, action, checked=None, default=False, visible=True):
        self.text, self.action, self.default, self.visible = text, action, default, visible


class FakePystray:
    Icon = FakeIcon
    MenuItem = FakeMenuItem

    class Menu:
        SEPARATOR = object()

        def __init__(self, *items):
            self.items = items


def test_hud_tray_menu_opens_hud_and_follows_state(monkeypatch):
    pytest.importorskip("PIL")
    import sys
    from jarvis.tray import Tray, follow_hud
    monkeypatch.setitem(sys.modules, "pystray", FakePystray)
    opened, quits, muted = [], [], [False]
    t = Tray(lambda: None, lambda: muted[0], lambda: None, lambda: quits.append(1),
             open_ui=lambda: opened.append(1), mic_available=lambda: True, look="hud")
    assert t.start() is True
    items = [i for i in t._icon.menu.items if isinstance(i, FakeMenuItem)]
    assert items[0].text == "Открыть HUD" and items[0].default
    assert not next(i for i in items if i.text.startswith("Микрофон")).default
    items[0].action()
    assert opened == [1]
    on_event = follow_hud(t, lambda: muted[0])
    on_event({"type": "state", "state": "thinking"})
    assert t.state == "thinking" and t._icon.title == "JARVIS — думаю"
    muted[0] = True
    on_event({"type": "state", "state": "idle"})
    assert t.state == "muted"
    on_event({"type": "notice", "text": "Пора пить воду", "reminder": True})
    on_event({"type": "notice", "text": "обычное сообщение"})
    assert t._icon.notified == ["Пора пить воду"]
    next(i for i in items if i.text == "Выход").action()
    assert quits == [1] and t._icon is None
