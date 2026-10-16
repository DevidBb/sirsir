"""Навыки: формат SKILL.md, выбор по фразе, ограничение инструментов, промпт."""

from pathlib import Path

import pytest

from conftest import FakeLLM
from jarvis import skills
from jarvis.assistant import Assistant
from jarvis.memory import Memory
from jarvis.skills import SkillLibrary, parse_skill
from jarvis.tool_registry import Param, Tool, ToolRegistry

ROOT = Path(__file__).resolve().parents[1]

SKILL = """---
name: demo
description: Проверочный навык.
triggers: ["сделай демо", "demo run"]
tools: [lookup, "files_*"]
max_steps: 50
---
Сначала вызови lookup.
"""


def write(folder: Path, name: str, text: str) -> None:
    (folder / name).mkdir(parents=True, exist_ok=True)
    (folder / name / "SKILL.md").write_text(text, encoding="utf-8")


def test_parse_skill_and_errors():
    s = parse_skill(SKILL)
    assert s.name == "demo" and s.triggers == ["сделай демо", "demo run"] and s.max_steps == 12
    assert s.allows("lookup") and s.allows("files_read") and not s.allows("browser_click")
    for bad, msg in [("нет заголовка", "заголовка"), ("---\nname: Плохое Имя\ndescription: x\n---\nтекст", "имя"),
                     ("---\nname: a\n---\nтекст", "description"), ("---\nname: a\ndescription: d\n---\n", "инструкции"),
                     ("---\nname: a\ndescription: d\nmax_steps: ноль\n---\nт", "max_steps")]:
        with pytest.raises(ValueError, match=msg):
            parse_skill(bad)


def test_bundled_skills_load(tmp_path):
    lib = SkillLibrary([ROOT / "skills"])
    assert {"research", "project-review", "video-pick"} <= set(lib.skills) and not lib.errors


def test_library_match_and_errors(tmp_path):
    write(tmp_path, "demo", SKILL)
    write(tmp_path, "broken", "без заголовка")
    lib = SkillLibrary([tmp_path])
    assert "broken" in lib.errors
    skill, task = lib.match("Джарвис, сделай демо про котов")
    assert skill.name == "demo" and task == "Джарвис, сделай демо про котов"
    skill, task = lib.match("навык demo: найди котов")
    assert skill.name == "demo" and task == "найди котов"
    assert lib.match("сделай демонстрацию") is None  # по границам слов
    assert lib.match("навык нет-такого что-то") is None


@pytest.fixture
def env(config, tmp_path):
    write(tmp_path, "demo", SKILL)
    registry = ToolRegistry()
    calls = []
    registry.register(Tool("lookup", "Найти", lambda q: calls.append(q) or f"нашёл {q}",
                           {"q": Param("string", "запрос")}))
    registry.register(Tool("browser_click", "Клик", lambda: calls.append("click") or "нажал"))
    lib = SkillLibrary([tmp_path])
    lib.register_tools(registry)
    return registry, lib, calls, config


def tool_call(name, args):
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}


def test_skill_limits_tools_and_adds_instructions(env):
    registry, lib, calls, config = env
    llm = FakeLLM([tool_call("browser_click", {}), tool_call("lookup", {"q": "коты"}),
                   {"role": "assistant", "content": "Готово."}])
    assistant = Assistant(config, llm, registry, Memory(":memory:"))
    assistant.skills = lib
    events = []
    assistant.on_event = events.append
    reply = assistant.handle("сделай демо про котов")
    assert reply.text == "Готово." and calls == ["коты"]  # клик вне навыка не выполнен
    first = llm.requests[0]
    assert [t["function"]["name"] for t in first["tools"]] == ["lookup"]
    system = first["messages"][0]["content"]
    assert "Сначала вызови lookup." in system and "навык «demo»" in system
    assert any(e["type"] == "skill" and e["name"] == "demo" for e in events)


def test_without_trigger_catalog_and_use_skill(env):
    registry, lib, _, config = env
    llm = FakeLLM([{"role": "assistant", "content": "Привет."}])
    assistant = Assistant(config, llm, registry, Memory(":memory:"))
    assistant.skills = lib
    assistant.handle("расскажи что-нибудь интересное про космос")
    names = [t["function"]["name"] for t in llm.requests[0]["tools"]]
    assert {"lookup", "browser_click", "use_skill"} <= set(names)
    assert "- demo: Проверочный навык." in llm.requests[0]["messages"][0]["content"]
    out = assistant._run_tool("use_skill", {"name": "demo"})
    assert "Сначала вызови lookup." in out and out.data["kind"] == "skill"
    assert assistant._run_tool("use_skill", {"name": "nope"}).ok is False


def test_setup(config):
    registry = ToolRegistry()
    lib = skills.setup(config, registry)
    assert "use_skill" in registry.names() and "research" in lib.skills
    config.set("skills.enabled", False)
    assert skills.setup(config, ToolRegistry()) is None
