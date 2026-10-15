import builtins

import pytest

from jarvis import autostart
from jarvis.tray import COLORS, Tray


def test_autostart_enable_disable(tmp_path):
    folder = tmp_path / "Startup"
    assert not autostart.is_enabled(folder)
    path = autostart.enable(tmp_path / "Проект", folder)
    text = path.read_text(encoding="utf-8")
    assert "chcp 65001" in text and "Проект" in text and "run.bat --tray" in text
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
