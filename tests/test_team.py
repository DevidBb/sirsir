import os
import sys
import threading
import time

import pytest

from conftest import FakeLLM, text
from jarvis import team as team_mod
from jarvis.assistant import Assistant
from jarvis.llm import LLMError
from jarvis.memory import Memory
from jarvis.team import CodexCLI, Member, Team, build_members, parse_team_request
from jarvis.tool_registry import ToolRegistry


# --- фразы ------------------------------------------------------------------------
@pytest.mark.parametrize("phrase, question", [
    ("Джарвис, спроси всех: какой ноутбук взять для монтажа?", "какой ноутбук взять для монтажа?"),
    ("спроси у всех ИИ, что лучше для кофейни — сайт или Instagram", "что лучше для кофейни — сайт или Instagram"),
    ("опроси всю команду как назвать барбершоп", "как назвать барбершоп"),
    ("обсудите командой: стоит ли открывать вторую точку", "стоит ли открывать вторую точку"),
    ("подумайте все вместе, сколько брать за стрижку", "сколько брать за стрижку"),
    ("все нейросети, придумайте слоган для пекарни", "придумайте слоган для пекарни"),
    ("совет ИИ: какой CRM выбрать", "какой CRM выбрать"),
    ("командой: план запуска лендинга", "план запуска лендинга"),
    ("пусть все ИИ обсудят, как поднять продажи", "как поднять продажи"),
    ("пусть команда подумает над названием кофейни", "над названием кофейни"),
])
def test_team_phrases(phrase, question):
    assert parse_team_request(phrase) == question


@pytest.mark.parametrize("phrase", ["спроси всех", "Джарвис, совет ИИ", "обсудите командой"])
def test_team_called_without_question(phrase):
    assert parse_team_request(phrase) == ""


@pytest.mark.parametrize("phrase", [
    "спроси у мамы, когда она придёт", "что такое командная строка", "открой команду", "сделай сайт для кофейни",
    "какие модели ноутбуков лучше", "все окна сверни",
])
def test_not_team_phrases(phrase):
    assert parse_team_request(phrase) is None


# --- ход работы ---------------------------------------------------------------------
def member(key, answer, delay=0.0, calls=None):
    def ask(system, prompt, stopped):
        if calls is not None:
            calls.append((key, system, prompt, threading.current_thread().name))
        time.sleep(delay)
        if isinstance(answer, Exception):
            raise answer
        return answer(system, prompt) if callable(answer) else answer
    return Member(key, team_mod.TITLES[key], ask)


def run_team(members, question="Как назвать кофейню?", cancelled=lambda: False, **kwargs):
    events, spoken = [], []
    t = Team(lambda: members, emit=events.append, **kwargs)
    outcome = t.run(question, spoken.append, cancelled)
    return outcome, events, spoken


def test_all_answer_in_parallel_and_lead_merges():
    calls = []
    lead_said = "Итог: «Зерно» — коротко и понятно. Разошлись: ChatGPT предлагал английское название."
    claude = member("claude", lambda s, p: lead_said if "ведущий" in s else "Назовите «Зерно».", 0.3, calls)
    members = [claude, member("codex", "Bean & Brew.", 0.3, calls), member("local", "Кофейня у дома.", 0.3, calls)]
    started = time.monotonic()
    outcome, events, spoken = run_team(members)
    assert time.monotonic() - started < 1.2                      # участники отвечают одновременно
    assert outcome.ok and outcome.text == lead_said
    assert spoken[0] == "Спрашиваю команду: Claude, ChatGPT и локальную модель."
    assert spoken[1] == "Все ответили. Claude сводит ответы."
    lead_call = calls[-1]
    assert lead_call[0] == "claude" and "ведущий" in lead_call[1]
    for answer in ("Назовите «Зерно».", "Bean & Brew.", "Кофейня у дома."):  # ведущий видит все ответы
        assert answer in lead_call[2]
    assert "Как назвать кофейню?" in lead_call[2]
    job = outcome.data["job"]
    assert [a["status"] for a in job["answers"]] == ["ok", "ok", "ok"] and job["lead"] == "claude"
    assert job["final"] == lead_said and events[-1]["type"] == "team"
    names = [e["name"] for e in events if e["type"] == "tool_end"]
    assert sorted(names[:3]) == ["team_claude", "team_codex", "team_local"] and names[-1] == "team_lead"


def test_failed_member_and_failed_lead_fall_back():
    calls = []
    members = [member("claude", LLMError("нет входа"), calls=calls),
               member("codex", lambda s, p: (_ for _ in ()).throw(LLMError("лимит")) if "ведущий" in s else "Ответ ChatGPT."),
               member("gemini", lambda s, p: "Итог от Gemini." if "ведущий" in s else "Ответ Gemini.")]
    outcome, _, spoken = run_team(members)
    assert outcome.text == "Итог от Gemini."
    job = outcome.data["job"]
    assert job["answers"][0]["status"] == "fail" and "нет входа" in job["answers"][0]["text"]
    assert job["lead"] == "gemini" and job["lead_status"] == "ok"
    assert "Ответили 2 из 3. ChatGPT сводит ответы." in spoken and "Ответили 2 из 3. Gemini сводит ответы." in spoken


def test_single_answer_or_nobody():
    outcome, _, _ = run_team([member("claude", LLMError("нет")), member("local", "Только я.")])
    assert outcome.ok and outcome.text == "Ответ есть только от локальной модели. Только я."
    outcome, _, spoken = run_team([member("local", "Один.")])
    assert outcome.text == "Один." and spoken == ["Сейчас в команде только локальная модель. Спрашиваю."]
    outcome, _, _ = run_team([member("claude", LLMError("нет входа")), member("codex", LLMError("лимит"))])
    assert not outcome.ok and "нет входа" in outcome.text and "лимит" in outcome.text
    outcome, _, _ = run_team([])
    assert not outcome.ok and "никого нет" in outcome.text


def test_stop_and_timeout():
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    started = time.monotonic()
    outcome, _, _ = run_team([member("claude", "долго", delay=5), member("local", "тоже")], cancelled=stop.is_set)
    assert outcome.text == "Остановил команду." and not outcome.ok and time.monotonic() - started < 2
    started = time.monotonic()
    outcome, _, _ = run_team([member("claude", "долго", delay=5), member("local", "быстро")], timeout=0.2, grace=0)
    assert outcome.text == "Ответ есть только от локальной модели. быстро" and time.monotonic() - started < 2
    assert outcome.data["job"]["answers"][0] == {"key": "claude", "title": "Claude", "status": "fail",
                                                 "text": "не ответил за 0 с", "seconds": 0.0}


def test_explicit_lead():
    said = {}
    def answer(key):
        return lambda s, p: said.setdefault(key, f"итог {key}") if "ведущий" in s else f"ответ {key}"
    outcome, _, _ = run_team([member("claude", answer("claude")), member("local", answer("local"))], lead="local")
    assert outcome.text == "итог local"


# --- Джарвис ---------------------------------------------------------------------
class FakeTeam:
    def __init__(self):
        self.asked = []

    def run(self, question, say, cancelled):
        self.asked.append(question)
        say("Спрашиваю команду.")
        return team_mod.Outcome("Итог команды.", {"kind": "team", "job": {"id": "t"}})


def test_assistant_routes_team_phrases(config):
    a = Assistant(config, FakeLLM([text("ответ модели")]), ToolRegistry(), Memory(config.path("memory.db_path")))
    assert a.handle("спроси всех: как назвать кофейню").text == "ответ модели"  # команды нет — обычный ответ
    a.team = FakeTeam()
    spoken = []
    reply = a.handle("Джарвис, спроси всех: как назвать кофейню?", on_sentence=lambda s: spoken.append(s) or True)
    assert reply.text == "Итог команды." and reply.data["kind"] == "team"
    assert a.team.asked == ["как назвать кофейню?"] and spoken == ["Спрашиваю команду."]
    assert "Что спросить у команды?" in a.handle("спроси всех").text
    assert a.conversation.messages("")[-1]["content"] == "Итог команды."


# --- участники ---------------------------------------------------------------------
FAKE_CODEX = r'''
import sys, pathlib
args = sys.argv[1:]
prompt = sys.stdin.read()
out = pathlib.Path(args[args.index("-o") + 1])
work = pathlib.Path(args[args.index("-C") + 1])
assert work.is_dir() and not any(work.iterdir()), "пустая рабочая папка"
assert args[:2] == ["exec", "--skip-git-repo-check"] and "read-only" in args and args[-1] == "-"
if "НЕ ВОШЁЛ" in prompt:
    print("Error: Not logged in. Run codex login", file=sys.stderr)
    sys.exit(1)
out.write_text("Ответ ChatGPT: " + prompt.splitlines()[-1] + " | " + " ".join(a for a in args if a == "-m" or a.startswith("gpt")), encoding="utf-8")
'''


@pytest.fixture
def fake_codex(tmp_path):
    script = tmp_path / "fake_codex.py"
    script.write_text(FAKE_CODEX, encoding="utf-8")
    if os.name == "nt":  # на ПК codex — это codex.cmd из npm
        exe = tmp_path / "codex.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        exe = tmp_path / "codex"
        exe.write_text(f"#!/bin/sh\nexec \"{sys.executable}\" \"{script}\" \"$@\"\n", encoding="utf-8")
        exe.chmod(0o755)
    return str(exe)


def test_codex_cli_answers_through_stdin_and_file(fake_codex):
    codex = CodexCLI(fake_codex, model="gpt-5.5", timeout=30)
    answer = codex.ask("Система.", "Как назвать кофейню? Ёлки «тест»")
    assert answer.startswith("Ответ ChatGPT: Как назвать кофейню? Ёлки «тест»") and "-m gpt-5.5" in answer
    with pytest.raises(LLMError, match="codex login"):
        codex.ask("Система.", "НЕ ВОШЁЛ")


def test_codex_cli_stops(tmp_path):
    script = tmp_path / "slow.py"
    script.write_text("import time; time.sleep(30)", encoding="utf-8")
    if os.name == "nt":
        exe = tmp_path / "slow.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        exe = tmp_path / "slow"
        exe.write_text(f"#!/bin/sh\nexec \"{sys.executable}\" \"{script}\"\n", encoding="utf-8")
        exe.chmod(0o755)
    started = time.monotonic()
    with pytest.raises(LLMError, match="не ответил"):
        CodexCLI(str(exe), timeout=0.5).ask("с", "в")
    assert time.monotonic() - started < 8


def test_build_members_only_available(config, monkeypatch):
    a = Assistant(config, FakeLLM([text("локальный ответ")]), ToolRegistry(), Memory(config.path("memory.db_path")))
    monkeypatch.setattr(team_mod.CodexCLI, "find", staticmethod(lambda: None))
    monkeypatch.setattr("jarvis.providers.ClaudeAgentBackend.available", staticmethod(lambda: (False, "нет")))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    t = team_mod.setup(config, a)
    assert [m.key for m in t.members()] == ["local"]
    monkeypatch.setattr(team_mod.CodexCLI, "find", staticmethod(lambda: "/usr/bin/codex"))
    monkeypatch.setenv("GEMINI_API_KEY", "ключ")
    monkeypatch.setattr("jarvis.providers.ClaudeAgentBackend.available", staticmethod(lambda: (True, "")))
    assert [m.key for m in t.members()] == ["claude", "codex", "gemini", "local"]
    config.set("team.members", ["codex", "local"])
    assert [m.key for m in build_members(config, a, t)] == ["codex", "local"]


def test_check_team_lists_found_and_missing(config, monkeypatch):
    from jarvis import checks
    monkeypatch.setattr(team_mod.CodexCLI, "find", staticmethod(lambda: "/usr/bin/codex"))
    monkeypatch.setattr("jarvis.providers.ClaudeAgentBackend.available", staticmethod(lambda: (False, "нет")))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    result = checks.check_team(config)[0]
    assert result.ok and not result.required
    assert result.detail.startswith("ChatGPT (codex), локальная модель; нет: Claude, Gemini")
    config.set("team.members", ["local"])
    result = checks.check_team(config)[0]
    assert not result.ok and result.detail == "локальная модель"  # одному не с кем сверяться
