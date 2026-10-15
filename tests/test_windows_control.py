from pathlib import Path

import pytest

from jarvis.tool_registry import ToolRegistry
from jarvis.windows_control import WindowsController


class FakeWindows:
    available = True

    def __init__(self):
        self.actions = []

    def titles(self):
        return ["Блокнот", "Telegram"]

    def act(self, query, action):
        if "блок" in query.lower():
            self.actions.append(action)
            return "Блокнот"
        return None


@pytest.fixture
def ctl(config):
    launched, opened, trashed = [], [], []
    c = WindowsController(config, launcher=launched.append, url_opener=opened.append,
                          window_backend=FakeWindows(), trash=trashed.append)
    c.launched, c.opened, c.trashed = launched, opened, trashed
    return c


def allowed_dir(config) -> Path:
    return Path(config.get("windows.search.allowed_dirs")[0])


def test_open_app_allowlist(ctl):
    assert "Запускаю" in ctl.open_app("Блокнот")
    assert ctl.launched == ["notepad.exe"]
    assert "не в списке" in ctl.open_app("cmd.exe /c del *")
    assert ctl.launched == ["notepad.exe"]


def test_open_website_allowlist(ctl):
    ctl.open_website("ютуб")
    assert ctl.opened == ["https://www.youtube.com"]
    assert "не в списке" in ctl.open_website("evil.example.com")
    assert len(ctl.opened) == 1


def test_search_files_only_in_allowed(ctl, config, tmp_path):
    base = allowed_dir(config)
    (base / "sub").mkdir()
    (base / "sub" / "Отчёт_2024.docx").write_text("x")
    (base / ".hidden").mkdir()
    (base / ".hidden" / "отчет_секрет.txt").write_text("x")
    (tmp_path / "отчет_снаружи.txt").write_text("x")
    result = ctl.search_files("отчет")
    assert "Отчёт_2024.docx" in result
    assert "снаружи" not in result and "секрет" not in result
    assert "не найдено" in ctl.search_files("несуществующий")


def test_delete_file_restricted(ctl, config, tmp_path):
    base = allowed_dir(config)
    inside = base / "a.txt"
    inside.write_text("x")
    outside = tmp_path / "b.txt"
    outside.write_text("x")
    assert "разрешённых" in ctl.delete_file(str(outside))
    assert "разрешённых" in ctl.delete_file(str(base / ".." / "b.txt"))
    assert ctl.trashed == []
    assert "корзину" in ctl.delete_file(str(inside))
    assert ctl.trashed == [str(inside.resolve())]


def test_windows_actions(ctl):
    assert "Блокнот" in ctl.list_windows()
    assert "Сворачиваю" in ctl.window_action("блокнот", "minimize")
    assert "не найдено" in ctl.window_action("word", "focus")


def test_system_info_and_time(ctl):
    assert "диске" in ctl.system_info()
    assert ctl.current_time().startswith("Сейчас")


def test_dangerous_tools_flagged(ctl):
    r = ToolRegistry()
    ctl.register_tools(r)
    assert r.get("delete_file").dangerous
    assert r.get("close_window").dangerous
    assert not r.get("open_app").dangerous
    # закрытие окна нельзя сделать через window_action в обход подтверждения
    from jarvis.tool_registry import ToolError
    with pytest.raises(ToolError):
        r.validate("window_action", {"title": "x", "action": "close"})


def test_media_control(config):
    pressed = []
    c = WindowsController(config, key_press=pressed.append)
    assert c.media_control("volume_up", 3) == "Громче."
    assert pressed == [0xAF] * 6
    pressed.clear()
    c.media_control("next", 5)
    assert pressed == [0xB0]


def test_media_control_unavailable(config):
    def fail(vk):
        raise OSError("только в Windows")
    c = WindowsController(config, key_press=fail)
    assert "Не получилось" in c.media_control("mute")


def test_current_time_is_speakable():
    import datetime as dt
    assert WindowsController.current_time(dt.datetime(2026, 10, 15, 9, 5)) == \
        "Сейчас 09:05, 15 октября, четверг."
