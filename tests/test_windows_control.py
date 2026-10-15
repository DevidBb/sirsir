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
    # Любой сайт по адресу — только https в браузере; прочие схемы и не-адреса — нет.
    assert ctl.open_website("example.com") == "Открываю example.com."
    assert ctl.opened[-1] == "https://example.com"
    for bad in ("javascript:alert(1)", "file:///C:/Windows", "C:\\Windows\\cmd.exe",
                "ftp://example.com", "user@evil.com", "какой-то сайт"):
        assert not ctl.open_website(bad).startswith("Открываю"), bad
    assert len(ctl.opened) == 2


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
    # закрыть окно можно только через close_window (с подтверждением)
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


def test_pygetwindow_false_error_is_success():
    from jarvis.windows_control import WindowBackend

    class Win:
        title = "Блокнот"
        isMinimized = False

        def activate(self):
            raise Exception("Error code from Windows: 0 - Операция успешно завершена.")

    class GW:
        @staticmethod
        def getAllWindows():
            return [Win()]

    b = WindowBackend()
    b._gw = GW
    assert b.act("блокнот", "focus") == "Блокнот"


@pytest.mark.parametrize("name", [" ", "а", "к", "ло", "!"])
def test_short_name_does_not_launch_random_app(ctl, name):
    # Модель иногда присылает огрызок имени: он не должен запускать первое попавшееся.
    assert "не в списке" in ctl.open_app(name)
    assert "не в списке" in ctl.open_website(name)
    assert ctl.launched == [] and ctl.opened == []


def test_partial_name_still_works(ctl):
    assert ctl.open_app("блокнот пожалуйста") == "Запускаю блокнот."
    assert ctl.open_app("блокн") == "Запускаю блокнот."
    assert ctl.launched == ["notepad.exe", "notepad.exe"]


def test_missing_program_gives_clear_message(config):
    def launcher(command):
        raise FileNotFoundError(2, "Не удается найти указанный файл", command)

    c = WindowsController(config, launcher=launcher)
    reply = c.open_app("блокнот")
    assert "не найден" in reply and "[windows.apps]" in reply and "Errno" not in reply


def test_browser_missing(config):
    c = WindowsController(config, url_opener=lambda url: False)
    assert "браузер" in c.open_website("ютуб")
