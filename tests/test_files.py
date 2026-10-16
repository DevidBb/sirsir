"""Файлы в разрешённых папках: чтение, поиск, изменения только после diff и подтверждения."""

import os
import sys

import pytest

from conftest import FakeLLM
from jarvis import files
from jarvis.assistant import Assistant, Reply
from jarvis.files import FileTools
from jarvis.memory import Memory
from jarvis.tool_registry import ToolRegistry


@pytest.fixture
def env(config, tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    (root / "README.md").write_bytes("Привет\r\nмир\r\n".encode("utf-8"))  # CRLF как есть
    (root / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("x", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("чужое", encoding="utf-8")
    tools = FileTools([root], tmp_path / "backups")
    registry = ToolRegistry()
    tools.register_tools(registry)
    assistant = Assistant(config, FakeLLM([]), registry, Memory(":memory:"))
    events = []
    assistant.on_event = events.append
    return tools, assistant, root, tmp_path, events


def test_read_list_search(env):
    tools, assistant, root, _, _ = env
    listing = assistant._run_tool("files_list", {})
    assert "src/" in listing and "README.md" in listing and "[секреты — недоступен]" in listing
    text = assistant._run_tool("files_read", {"path": "src/app.py"})
    assert "    1  def main():" in text and "<untrusted>" in text
    found = assistant._run_tool("files_search", {"query": "return"})
    assert "app.py:2" in found and ".git" not in found


def test_paths_outside_and_secrets_refused(env):
    tools, assistant, root, tmp_path, _ = env
    assert "вне разрешённых" in assistant._run_tool("files_read", {"path": str(tmp_path / "outside.txt")})
    assert "вне разрешённых" in assistant._run_tool("files_read", {"path": "../outside.txt"})
    assert "секреты" in assistant._run_tool("files_read", {"path": ".env"})
    assert "секреты" not in tools.search("TOKEN")  # .env не попадает в поиск
    assert "не найдено" in tools.search("TOKEN")


@pytest.mark.skipif(sys.platform == "win32", reason="символьные ссылки на Windows требуют прав")
def test_symlink_escape_refused(env):
    tools, _, root, tmp_path, _ = env
    os.symlink(tmp_path / "outside.txt", root / "link.txt")
    assert "вне разрешённых" in tools.read_file("link.txt")


def test_edit_needs_confirmation_with_diff_and_backup(env):
    tools, assistant, root, tmp_path, events = env
    reply = assistant._run_tool("files_edit", {"path": "src/app.py", "old_text": "return 1",
                                               "new_text": "return 2"})
    assert isinstance(reply, Reply) and f"изменить файл {os.path.join('proj', 'src', 'app.py')} (+1 −1 строк)" in reply.text
    assert "return 1" in (root / "src" / "app.py").read_text()  # до подтверждения не меняется
    confirm = [e for e in events if e["type"] == "confirm"][0]
    assert confirm["details"]["kind"] == "diff" and "+    return 2" in confirm["details"]["diff"]
    done = assistant.handle("подтверждаю")
    assert "Изменил" in done.text and done.data["kind"] == "file_changed"
    assert (root / "src" / "app.py").read_text() == "def main():\n    return 2\n"
    backups = list((tmp_path / "backups").iterdir())
    assert len(backups) == 1 and "return 1" in backups[0].read_text()


def test_cancel_leaves_file(env):
    _, assistant, root, _, _ = env
    assistant._run_tool("files_write", {"path": "src/app.py", "content": "пусто"})
    assert assistant.handle("отмена").text == "Действие отменено."
    assert "return 1" in (root / "src" / "app.py").read_text()


def test_file_changed_after_diff_is_not_overwritten(env):
    _, assistant, root, _, _ = env
    assistant._run_tool("files_edit", {"path": "src/app.py", "old_text": "return 1", "new_text": "return 2"})
    (root / "src" / "app.py").write_text("def main():\n    return 1  # правка пользователя\n", encoding="utf-8")
    done = assistant.handle("подтверждаю")
    assert "изменился после показа" in done.text
    assert "правка пользователя" in (root / "src" / "app.py").read_text(encoding="utf-8")


def test_precheck_errors_skip_confirmation(env):
    _, assistant, _, _, _ = env
    out = assistant._run_tool("files_edit", {"path": "src/app.py", "old_text": "нет такого", "new_text": "x"})
    assert not isinstance(out, Reply) and "не найден" in out
    assert not assistant.confirmations.has_pending()
    assert "не создаёт" in assistant._run_tool("files_write", {"path": "run.bat", "content": "del *"})
    assert "git" in assistant._run_tool("files_write", {"path": ".git/config", "content": "y"})
    assert "изменений нет" in assistant._run_tool("files_write", {"path": "src/app.py",
                                                                 "content": "def main():\n    return 1\n"})


def test_create_file_and_keep_crlf(env):
    _, assistant, root, _, _ = env
    assistant._run_tool("files_write", {"path": "notes/new.txt", "content": "строка\nещё"})
    assert "Создал" in assistant.handle("подтверждаю").text
    assert (root / "notes" / "new.txt").read_text(encoding="utf-8") == "строка\nещё"
    assistant._run_tool("files_edit", {"path": "README.md", "old_text": "мир", "new_text": "мир!"})
    assistant.handle("подтверждаю")
    assert (root / "README.md").read_bytes() == "Привет\r\nмир!\r\n".encode("utf-8")


def test_confirmation_waits_longer_for_diff(env):
    tools, assistant, _, _, _ = env
    now = [0.0]
    assistant.confirmations.clock = lambda: now[0]
    assistant._run_tool("files_write", {"path": "a.txt", "content": "x"})
    now[0] = 60  # обычное подтверждение истекло бы через 20 с
    assert assistant.confirmations.has_pending()
    now[0] = files.CONFIRM_TIMEOUT + 1
    assert not assistant.confirmations.has_pending()


def test_setup_from_config(config, tmp_path):
    registry = ToolRegistry()
    assert files.setup(config, registry) is None and "files_read" not in registry.names()
    config.set("files.allowed_dirs", [str(tmp_path), str(tmp_path / "нет")])
    tools = files.setup(config, registry)
    assert "files_edit" in registry.names() and tools.missing == [str(tmp_path / "нет")]
