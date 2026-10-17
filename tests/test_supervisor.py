"""jarvis.supervisor: перезапуск после «Обновить и перезапустить» в HUD."""

import sys

from jarvis import supervisor
from jarvis.supervisor import RESTART_EXIT_CODE, supervise


def fake_runner(codes):
    calls = []

    def run(cmd):
        calls.append(cmd)
        if cmd[2] == "jarvis.updater":
            return 0
        return codes.pop(0)
    return run, calls


def test_normal_exit_passes_code_through():
    run, calls = fake_runner([0])
    assert supervise(["--web", "--voice"], python="py", run=run, out=lambda s: None) == 0
    assert calls == [["py", "-m", "jarvis", "--web", "--voice"]]


def test_restart_runs_updater_then_jarvis_without_new_tab(monkeypatch):
    monkeypatch.delenv(supervisor.DRY_RUN_ENV, raising=False)
    run, calls = fake_runner([RESTART_EXIT_CODE, 1])
    assert supervise(["--web"], python="py", run=run, out=lambda s: None) == 1
    assert calls == [["py", "-m", "jarvis", "--web"],
                     ["py", "-m", "jarvis.updater", "--from-hud"],
                     ["py", "-m", "jarvis", "--web", "--no-open"]]


def test_dry_run_env_for_smoke_tests(monkeypatch):
    monkeypatch.setenv(supervisor.DRY_RUN_ENV, "1")
    run, calls = fake_runner([RESTART_EXIT_CODE, 0])
    supervise(["--web", "--no-open"], python="py", run=run, out=lambda s: None)
    assert calls[1] == ["py", "-m", "jarvis.updater", "--from-hud", "--dry-run"]
    assert calls[2].count("--no-open") == 1


def test_restart_loop_is_bounded():
    run, calls = fake_runner([RESTART_EXIT_CODE] * (supervisor.MAX_RESTARTS + 5))
    assert supervise([], python="py", run=run, out=lambda s: None) == 1
    assert len([c for c in calls if c[2] == "jarvis"]) == supervisor.MAX_RESTARTS + 1


def test_real_child_process_and_env(tmp_path, monkeypatch):
    """Настоящий дочерний процесс: код выхода и признак супервизора в окружении."""
    script = tmp_path / "child.py"
    script.write_text("import os, sys; sys.exit(7 if os.environ.get('JARVIS_SUPERVISED') == '1' else 3)",
                      encoding="utf-8")
    assert supervisor.run_child([sys.executable, str(script)]) == 7
