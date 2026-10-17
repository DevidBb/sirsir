"""Обновление (jarvis/updater.py) на настоящих git-репозиториях во временной папке."""

import datetime as dt
import os
import shutil
import subprocess
import tomllib

import pytest

from jarvis.config import Config, diff, dumps_toml, merge
from jarvis.updater import Updater

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="нужен git")

BASE_CONFIG = """[llm]
provider = "hybrid"
model = "qwen3:8b"

[files]
allowed_dirs = []

[windows.apps]
notepad = "notepad.exe"
"""


def git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    out = subprocess.run(["git", "-c", "init.defaultBranch=main", "-c", "core.autocrlf=false",
                          *args], cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))  # без \r\n → \r\r\n на Windows


@pytest.fixture
def repos(tmp_path):
    """upstream (откуда пушит разработчик), origin.git и clone — установка владельца."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", str(origin))
    upstream = tmp_path / "upstream"
    git(tmp_path, "clone", str(origin), str(upstream))
    write(upstream / "config.toml", BASE_CONFIG)
    write(upstream / "requirements.txt", "numpy\n")
    write(upstream / "README.md", "v1\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "v1")
    git(upstream, "push", "origin", "HEAD:main")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-b", "main", str(origin), str(clone))
    return upstream, clone


def push(upstream, files, message="v2"):
    for name, text in files.items():
        write(upstream / name, text)
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", message)
    git(upstream, "push", "origin", "HEAD:main")


def make_updater(clone, installed=()):
    lines, commands = [], []

    def run_live(cmd, cwd):
        commands.append(cmd)
        return 0

    upd = Updater(root=clone, python="py", out=lines.append, run_live=run_live,
                  installed=lambda m: m in installed,
                  now=lambda: dt.datetime(2026, 10, 17, 12, 0, 0))
    return upd, lines, commands


def test_already_latest(repos):
    _, clone = repos
    upd, _, commands = make_updater(clone)
    result = upd.update()
    assert result.code == 0 and "последняя версия" in result.message
    assert commands == []


def test_fast_forward_without_requirements(repos):
    upstream, clone = repos
    push(upstream, {"README.md": "v2\n"}, "Новая функция")
    upd, lines, commands = make_updater(clone)
    result = upd.update()
    assert result.code == 0, result.message
    assert (clone / "README.md").read_text(encoding="utf-8") == "v2\n"
    assert any("Новая функция" in line for line in lines)
    assert commands == []  # requirements не менялись — pip не запускается


def test_requirements_changed_runs_pip_and_playwright(repos):
    upstream, clone = repos
    push(upstream, {"requirements.txt": "numpy\nmcp\n", "requirements-claude.txt": "sdk\n",
                    "requirements-silero.txt": "torch\n"})
    upd, _, commands = make_updater(clone, installed={"claude_agent_sdk"})
    assert upd.update().code == 0
    flat = [" ".join(c) for c in commands]
    assert "py -m pip install -r requirements.txt" in flat
    assert "py -m playwright install chromium" in flat
    assert "py -m pip install -r requirements-claude.txt" in flat
    assert not any("silero" in c for c in flat)  # Silero не стоит — не ставим без спроса


def test_pip_failure_is_reported(repos):
    upstream, clone = repos
    push(upstream, {"requirements.txt": "numpy\nmcp\n"})
    upd, _, _ = make_updater(clone)
    upd.run_live = lambda cmd, cwd: 1
    result = upd.update()
    assert result.code == 1 and "не все" in result.message


def test_config_edits_move_to_local_file(repos):
    upstream, clone = repos
    write(clone / "config.toml", BASE_CONFIG.replace('allowed_dirs = []',
                                                     'allowed_dirs = ["D:\\\\Проекты"]')
          .replace('notepad = "notepad.exe"', 'notepad = "notepad.exe"\nobs = "C:\\\\obs.exe"'))
    push(upstream, {"config.toml": BASE_CONFIG.replace('model = "qwen3:8b"',
                                                       'model = "qwen3:14b"')})
    upd, lines, _ = make_updater(clone)
    result = upd.update()
    assert result.code == 0, result.message

    local = tomllib.loads((clone / "config.local.toml").read_text(encoding="utf-8"))
    assert local == {"files": {"allowed_dirs": ["D:\\Проекты"]},
                     "windows": {"apps": {"obs": "C:\\obs.exe"}}}
    assert "qwen3:14b" in (clone / "config.toml").read_text(encoding="utf-8")
    assert (clone / "data" / "config.toml.20261017-120000.bak").is_file()
    assert any("config.local.toml" in line for line in lines)

    cfg = Config.load(clone / "config.toml", local=True)
    assert cfg.get("llm.model") == "qwen3:14b"          # новое из репозитория
    assert cfg.get("files.allowed_dirs") == ["D:\\Проекты"]  # своё сохранилось
    assert cfg.get("windows.apps") == {"notepad": "notepad.exe", "obs": "C:\\obs.exe"}
    assert cfg.local_path == clone / "config.local.toml"
    assert Config.load(clone / "config.toml").get("files.allowed_dirs") == []  # явный путь — без личного


def test_config_edits_merge_into_existing_local(repos):
    upstream, clone = repos
    write(clone / "config.local.toml", '[llm]\nprovider = "ollama"\nmodel = "my"\n')
    write(clone / "config.toml", BASE_CONFIG.replace('model = "qwen3:8b"', 'model = "fresh"'))
    upd, _, _ = make_updater(clone)
    assert upd.update().code == 0
    local = tomllib.loads((clone / "config.local.toml").read_text(encoding="utf-8"))
    assert local == {"llm": {"provider": "ollama", "model": "fresh"}}


def test_broken_config_stops_update(repos):
    upstream, clone = repos
    write(clone / "config.toml", BASE_CONFIG + "\n[broken\n")
    push(upstream, {"README.md": "v2\n"})
    upd, _, _ = make_updater(clone)
    result = upd.update()
    assert result.code == 1 and "config.toml" in result.message
    assert (clone / "README.md").read_text(encoding="utf-8") == "v1\n"


def test_other_modified_files_are_not_touched(repos):
    upstream, clone = repos
    write(clone / "README.md", "моя правка\n")
    push(upstream, {"requirements.txt": "numpy\nmcp\n"})
    upd, _, commands = make_updater(clone)
    result = upd.update()
    assert result.code == 1 and "README.md" in result.message
    assert (clone / "README.md").read_text(encoding="utf-8") == "моя правка\n"
    assert commands == []


def test_diverged_history_is_not_merged(repos):
    upstream, clone = repos
    push(upstream, {"README.md": "v2\n"})
    write(clone / "notes.txt", "local\n")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "локальный коммит")
    upd, _, _ = make_updater(clone)
    result = upd.update()
    assert result.code == 1 and "разошлась" in result.message
    assert (clone / "README.md").read_text(encoding="utf-8") == "v1\n"


def test_not_a_git_checkout(tmp_path):
    upd, _, _ = make_updater(tmp_path)
    result = upd.update()
    assert result.code == 1 and "git clone" in result.message


def test_toml_roundtrip_and_diff():
    data = {"a": 1, "s": 'кавычка " и \\ слэш\nстрока', "b": True, "f": 0.5, "l": ["x", 2],
            "t": {"k": "v", "inner": {"z": []}}, "empty": {}, "key with space": {"q": 1}}
    assert tomllib.loads(dumps_toml(data, "заголовок\nвторая строка")) == data

    changed, removed = diff({"x": {"a": 1, "b": 2}, "gone": 1}, {"x": {"a": 1, "b": 3, "c": 4}})
    assert changed == {"x": {"b": 3, "c": 4}} and sorted(removed) == ["gone"]
    assert merge({"x": {"a": 1}}, {"x": {"b": 2}, "y": 3}) == {"x": {"a": 1, "b": 2}, "y": 3}


def test_broken_local_config_has_clear_error(tmp_path):
    write(tmp_path / "config.toml", BASE_CONFIG)
    write(tmp_path / "config.local.toml", "[oops\n")
    with pytest.raises(ValueError, match="config.local.toml"):
        Config.load(tmp_path / "config.toml", local=True)


def test_report_shows_code_version_and_local_sections(repos):
    from jarvis import report
    _, clone = repos
    write(clone / "config.local.toml", '[files]\nallowed_dirs = ["D:/секрет"]\n')
    cfg = Config.load(clone / "config.toml", local=True)
    assert report._local_config(cfg) == "разделы: files"  # без значений
    assert report._code_version(clone).split()[0] == git(clone, "rev-parse", "--short", "HEAD")
    assert report._local_config(Config.load(clone / "config.toml")) == "нет"


# --- проверка для HUD: только смотрит, ничего не меняет ---

def test_check_up_to_date(repos):
    _, clone = repos
    st = Updater(root=clone, interactive=False).check()
    assert st["error"] is None and st["available"] is False and st["can_apply"] is False
    assert st["current"]["subject"] == "v1" and len(st["current"]["sha"]) >= 7


def test_check_finds_updates_without_changing_anything(repos):
    upstream, clone = repos
    push(upstream, {"README.md": "v2\n"}, "Первое")
    push(upstream, {"requirements.txt": "numpy\nmcp\n"}, "Второе")
    head = git(clone, "rev-parse", "HEAD")
    st = Updater(root=clone, interactive=False).check()
    assert st["available"] and st["behind"] == 2 and st["can_apply"]
    assert [c["subject"] for c in st["commits"]] == ["Второе", "Первое"]
    assert st["needs_packages"] is True
    assert git(clone, "rev-parse", "HEAD") == head  # ветка не сдвинулась
    assert (clone / "README.md").read_text(encoding="utf-8") == "v1\n"


def test_check_reports_blocking_edits_and_config(repos):
    upstream, clone = repos
    push(upstream, {"README.md": "v2\n"})
    write(clone / "README.md", "моё\n")
    write(clone / "config.toml", BASE_CONFIG.replace("qwen3:8b", "my"))
    st = Updater(root=clone, interactive=False).check()
    assert st["blocked"] == ["README.md"] and st["config_edited"] is True
    assert st["available"] and not st["can_apply"]
    assert st["needs_packages"] is False


def test_check_diverged(repos):
    upstream, clone = repos
    push(upstream, {"README.md": "v2\n"})
    write(clone / "notes.txt", "local\n")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "своё")
    st = Updater(root=clone, interactive=False).check()
    assert st["behind"] == 1 and st["ahead"] == 1 and not st["can_apply"]


def test_check_fetch_error_and_not_git(repos, tmp_path):
    _, clone = repos
    git(clone, "remote", "set-url", "origin", str(tmp_path / "нет-такого.git"))
    st = Updater(root=clone, interactive=False).check()
    assert st["error"] and "fetch" in st["error"] and not st["available"]
    assert "git clone" in Updater(root=tmp_path / "empty", interactive=False).check()["error"]


def test_background_git_never_prompts(monkeypatch, tmp_path):
    import jarvis.updater as up
    seen = {}

    def fake(cmd, cwd, env=None, timeout=None):
        seen.update(env=env, timeout=timeout)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(up, "_run_capture", fake)
    Updater(root=tmp_path, interactive=False).fetch()
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0" and seen["env"]["GCM_INTERACTIVE"] == "never"
    assert seen["timeout"] == up.FETCH_TIMEOUT
    Updater(root=tmp_path).fetch()  # update.bat в консоли может спросить вход
    assert seen["env"] is None


def test_git_timeout_is_a_clear_error(monkeypatch, tmp_path):
    import jarvis.updater as up

    def slow(cmd, cwd, env=None, timeout=None):
        raise subprocess.TimeoutExpired(cmd, timeout)
    monkeypatch.setattr(up, "_run_capture", slow)
    with pytest.raises(up.UpdateError, match="нет ответа"):
        Updater(root=tmp_path, interactive=False).fetch()


# --- обновление по кнопке HUD: итог для показа после перезапуска ---

def test_run_from_hud_writes_result_for_hud(repos):
    from jarvis.updater import run_from_hud, take_result
    upstream, clone = repos
    push(upstream, {"README.md": "v2\n"}, "Новая функция")
    upd, _, _ = make_updater(clone)
    assert run_from_hud(upd) == 0
    assert (clone / "README.md").read_text(encoding="utf-8") == "v2\n"
    result = take_result(clone)
    assert result["ok"] and result["from"]["subject"] == "v1" and result["to"]["subject"] == "Новая функция"
    assert [c["subject"] for c in result["commits"]] == ["Новая функция"]
    assert take_result(clone) is None  # показывается один раз


def test_run_from_hud_dry_run_changes_nothing(repos):
    from jarvis.updater import run_from_hud, take_result
    upstream, clone = repos
    push(upstream, {"README.md": "v2\n"})
    upd, _, _ = make_updater(clone)
    assert run_from_hud(upd, dry_run=True) == 0
    assert (clone / "README.md").read_text(encoding="utf-8") == "v1\n"
    result = take_result(clone)
    assert result["dry_run"] and "новых коммитов 1" in result["message"] and result["commits"] == []


def test_failed_update_result_and_stale_results(repos):
    import json
    import time
    from jarvis.updater import result_path, run_from_hud, take_result, write_result
    upstream, clone = repos
    push(upstream, {"README.md": "v2\n"})
    write(clone / "README.md", "моё\n")
    upd, _, _ = make_updater(clone)
    assert run_from_hud(upd) == 1
    result = take_result(clone)
    assert result["ok"] is False and "README.md" in result["message"]
    write_result({"at": time.time() - 7200, "ok": True, "message": "давно"}, clone)
    assert take_result(clone) is None and not result_path(clone).exists()
    result_path(clone).write_text(json.dumps({"at": "мусор"}), encoding="utf-8")
    assert take_result(clone) is None


def test_update_checker_publishes_status(repos):
    from jarvis.updater import UpdateChecker
    upstream, clone = repos
    push(upstream, {"README.md": "v2\n"})
    events = []
    checker = UpdateChecker(Updater(root=clone, interactive=False), events.append, first_delay=0)
    st = checker.check_now()
    assert st["behind"] == 1 and checker.status is st
    assert events[-1]["type"] == "update" and events[-1]["behind"] == 1
