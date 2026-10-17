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
