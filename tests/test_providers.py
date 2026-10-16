"""Провайдер Claude Agent SDK с подставным клиентом (без сети и без авторизации)."""

import asyncio

import pytest

pytest.importorskip("claude_agent_sdk")
import claude_agent_sdk  # noqa: E402
from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, TextBlock, ToolUseBlock  # noqa: E402

from conftest import FakeLLM  # noqa: E402
from jarvis.assistant import STOP, Assistant  # noqa: E402
from jarvis.memory import Memory  # noqa: E402
from jarvis.providers import ClaudeAgentBackend, make_agent_backend  # noqa: E402
from jarvis.tool_registry import Param, Tool, ToolRegistry, ToolResult  # noqa: E402


def result(text, is_error=False, subtype="success"):
    return ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=is_error, num_turns=1,
                         session_id="sess-1", result=text)


class FakeClient:
    """Сценарий: список шагов — ("tool", имя, аргументы) или готовые сообщения SDK."""

    script = []
    seen_options = []
    prompts = []
    closed = 0
    hang = False

    def __init__(self, options):
        self.options = options
        FakeClient.seen_options.append(options)
        self.interrupted = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        FakeClient.closed += 1
        return False

    async def query(self, prompt):
        self.prompt = prompt
        FakeClient.prompts.append(prompt)

    async def interrupt(self):
        self.interrupted = True

    async def receive_response(self):
        tools = self.options.mcp_servers["jarvis"]["tools"]
        for step in FakeClient.script:
            if isinstance(step, tuple) and step[0] == "tool":
                out = await tools[step[1]].handler(step[2])
                yield StreamEvent(uuid="u", session_id="s", event={
                    "type": "content_block_delta", "delta": {"type": "text_delta",
                                                             "text": out["content"][0]["text"][:20]}})
            else:
                yield step
        while FakeClient.hang and not self.interrupted:
            await asyncio.sleep(0.05)
        if self.interrupted:
            yield result("", is_error=True, subtype="error_during_execution")


@pytest.fixture
def env(config, monkeypatch):
    monkeypatch.setattr(claude_agent_sdk, "create_sdk_mcp_server",
                        lambda name, tools=None, version="1": {"tools": {t.name: t for t in tools}})
    FakeClient.script, FakeClient.seen_options, FakeClient.hang = [], [], False
    FakeClient.prompts, FakeClient.closed = [], 0
    registry = ToolRegistry()
    calls = []
    registry.register(Tool("lookup", "Найти", lambda q: (calls.append(q), ToolResult(
        f"нашёл {q}", {"kind": "web", "items": []}))[1], {"q": Param("string", "запрос")}))
    registry.register(Tool("wipe", "Удалить всё", lambda: calls.append("wipe") or "удалено",
                           dangerous=True, confirm_template="удалить всё"))
    assistant = Assistant(config, FakeLLM([]), registry, Memory(":memory:"))
    events = []
    assistant.on_event = events.append
    backend = ClaudeAgentBackend(client_factory=FakeClient, work_dir=config.root / "data" / "test-agent")
    assistant.agent_backend = backend
    yield assistant, backend, calls, events
    backend.close()


def test_agent_uses_registry_tools_with_isolation(env):
    assistant, backend, calls, events = env
    FakeClient.script = [("tool", "lookup", {"q": "mcp"}),
                         AssistantMessage(content=[TextBlock("Готово: MCP")], model="m"),
                         result("Готово: MCP")]
    reply = assistant.handle("расскажи подробно что такое mcp")
    assert calls == ["mcp"]
    assert reply.text == "Готово: MCP" and reply.data["kind"] == "web"
    opts = FakeClient.seen_options[0]
    # Встроенные инструменты Claude Code и пользовательские настройки отключены.
    assert opts.tools == [] and opts.setting_sources == [] and opts.strict_mcp_config is True
    assert opts.allowed_tools == ["mcp__jarvis__lookup", "mcp__jarvis__wipe"]
    assert backend.session_id == "sess-1"
    assert any(e["type"] == "delta" for e in events)


def test_agent_tool_validation_errors_reach_model(env):
    assistant, _, calls, _ = env
    FakeClient.script = [("tool", "lookup", {"q": "x", "evil": 1}), result("не вышло")]
    reply = assistant.handle("найди что нибудь сложное и проанализируй")
    assert calls == [] and reply.text == "не вышло"


def test_agent_dangerous_tool_waits_for_confirmation(env):
    assistant, _, calls, _ = env
    FakeClient.script = [("tool", "wipe", {}), result("Нужно ваше подтверждение.")]
    reply = assistant.handle("удали вообще всё что есть")
    assert calls == [] and "удалить всё" in reply.text
    assert assistant.confirmations.has_pending()
    done = assistant.handle("подтверждаю")
    assert calls == ["wipe"] and "удалено" in done.text


def test_agent_error_is_reported_honestly(env):
    assistant, backend, _, _ = env
    FakeClient.script = [result("Invalid API key", is_error=True, subtype="error")]
    reply = assistant.handle("сделай что то сложное пожалуйста")
    assert "Claude недоступна" in reply.text and "Invalid API key" in reply.text


def test_agent_cancel_stops_without_false_error(env):
    assistant, _, _, _ = env
    FakeClient.hang = True
    import threading
    threading.Timer(0.3, assistant.emergency_stop).start()
    reply = assistant.handle("долгая задача на много шагов")
    assert reply.text == "Остановлено." and reply.action == STOP


def test_make_agent_backend_by_config(config, monkeypatch):
    config.set("llm.provider", "ollama")
    assert make_agent_backend(config) is None
    config.set("llm.provider", "claude")
    config.set("llm.claude.model", "haiku")
    backend = make_agent_backend(config)
    assert isinstance(backend, ClaudeAgentBackend) and backend.model == "haiku"


def test_browser_screenshot_is_shown_to_model(env, tmp_path):
    from PIL import Image
    assistant, _, _, _ = env
    shot = tmp_path / "shot.png"
    Image.new("RGB", (100, 50), (255, 255, 255)).save(shot)
    assistant.registry.register(Tool("snap", "Снимок страницы", lambda: ToolResult(
        "Снимок страницы сохранён", {"kind": "screenshot", "file": "shot.png", "image_path": str(shot)})))
    outputs = []

    class Capture(FakeClient):
        async def receive_response(self):
            out = await self.options.mcp_servers["jarvis"]["tools"]["snap"].handler({})
            outputs.append(out)
            yield result("Белая страница.")
    assistant.agent_backend._client_factory = Capture
    assistant.handle("сделай снимок страницы и опиши подробно что там")
    kinds = [c["type"] for c in outputs[0]["content"]]
    assert kinds == ["text", "image"] and outputs[0]["content"][1]["mimeType"] == "image/jpeg"


def test_user_image_is_sent_with_prompt(env):
    assistant, backend, _, _ = env
    prompts = []

    class Capture(FakeClient):
        async def query(self, prompt):
            prompts.append([m async for m in prompt])
    FakeClient.script = [result("Вижу экран.")]
    backend._client_factory = Capture
    reply = backend.run(assistant, "что на экране", None, image=("QUJD", "image/jpeg"))
    content = prompts[0][0]["message"]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["data"] == "QUJD"
    assert content[1]["text"].endswith("что на экране") and reply.text == "Вижу экран."


def test_skill_restricts_claude_tools(env, tmp_path):
    from jarvis.skills import SkillLibrary
    assistant, _, calls, _ = env
    (tmp_path / "sk" / "only-lookup").mkdir(parents=True)
    (tmp_path / "sk" / "only-lookup" / "SKILL.md").write_text(
        "---\nname: only-lookup\ndescription: Только поиск.\ntriggers: [поищи по навыку]\ntools: [lookup]\n---\n"
        "Ищи аккуратно.\n", encoding="utf-8")
    assistant.skills = SkillLibrary([tmp_path / "sk"])
    FakeClient.script = [result("Нашёл.")]
    assistant.handle("поищи по навыку что такое mcp")
    opts = FakeClient.seen_options[0]
    assert opts.allowed_tools == ["mcp__jarvis__lookup"]
    assert "Ищи аккуратно." in opts.system_prompt


def test_delegation_agents_are_read_only_and_builtin_agents_denied(env):
    from claude_agent_sdk import AgentDefinition, PermissionResultAllow, PermissionResultDeny
    assistant, _, calls, events = env
    for name in ("internet_search", "fetch_page"):
        assistant.registry.register(Tool(name, name, lambda **a: "ok", {"q": Param("string", "q", required=False)}))
    FakeClient.script = [
        AssistantMessage(content=[ToolUseBlock("t1", "Agent", {"subagent_type": "researcher",
                                                               "description": "цены в Германии"})], model="m"),
        AssistantMessage(content=[TextBlock("текст агента")], model="m", parent_tool_use_id="t1"),
        result("Сводка.")]
    reply = assistant.handle("сравни цены на iphone в трёх странах и дай вывод")
    assert reply.text == "Сводка."
    opts = FakeClient.seen_options[0]
    assert opts.tools == ["Agent"] and list(opts.agents) == ["researcher"]
    agent = opts.agents["researcher"]
    assert isinstance(agent, AgentDefinition)
    assert agent.tools == ["mcp__jarvis__internet_search", "mcp__jarvis__fetch_page"]  # wipe недоступен
    assert "Agent" not in opts.allowed_tools  # проходит через can_use_tool
    assert agent.background is False and opts.env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
    assert any(e["type"] == "delegate" and e["agent"] == "researcher" for e in events)
    assert not any(e["type"] == "delta" and "агента" in e.get("text", "") for e in events)
    check = opts.can_use_tool
    assert isinstance(asyncio.run(check("Agent", {"subagent_type": "researcher"}, None)), PermissionResultAllow)
    for name, inp in [("Agent", {"subagent_type": "general-purpose"}), ("Agent", {}), ("Bash", {"command": "ls"})]:
        assert isinstance(asyncio.run(check(name, inp, None)), PermissionResultDeny)


def test_delegation_can_be_disabled(env):
    assistant, backend, _, _ = env
    assistant.registry.register(Tool("fetch_page", "f", lambda **a: "ok"))
    backend.delegate = False
    FakeClient.script = [result("ок")]
    assistant.handle("сравни подробно два варианта и дай вывод")
    assert FakeClient.seen_options[0].tools == [] and not FakeClient.seen_options[0].agents


def test_client_is_reused_between_turns(env):
    assistant, backend, _, _ = env
    FakeClient.script = [result("раз")]
    assistant.handle("расскажи подробно про первую вещь")
    FakeClient.script = [result("два")]
    reply = assistant.handle("а теперь подробно про вторую вещь")
    assert reply.text == "два"
    assert len(FakeClient.seen_options) == 1 and FakeClient.closed == 0  # Claude Code не перезапускался
    assert len(FakeClient.prompts) == 2


def test_client_reconnects_on_new_session_and_after_error(env):
    assistant, backend, _, _ = env
    FakeClient.script = [result("раз")]
    assistant.handle("расскажи подробно про первую вещь")
    backend.reset()  # «новый диалог»
    assistant.handle("расскажи подробно про вторую вещь")
    assert len(FakeClient.seen_options) == 2 and FakeClient.seen_options[1].resume is None
    FakeClient.script = [result("Invalid API key", is_error=True, subtype="error")]
    assert "Claude недоступна" in assistant.handle("сделай что то сложное пожалуйста").text
    FakeClient.script = [result("снова работает")]
    assert assistant.handle("сделай что то сложное ещё раз").text == "снова работает"
    assert len(FakeClient.seen_options) == 3 and FakeClient.seen_options[2].resume == "sess-1"


def test_query_memory_goes_to_prompt_not_system_prompt(env):
    assistant, backend, _, _ = env
    assistant.memory.modify("INSERT INTO notes(text, created_at) VALUES(?, ?)", ("рецепт борща: свёкла, капуста", 1.0))
    FakeClient.script = [result("ок")]
    assistant.handle("расскажи подробно как варить борщ по рецепту")
    assert "свёкла" in FakeClient.prompts[0] and "свёкла" not in FakeClient.seen_options[0].system_prompt


def test_warm_up_connects_without_query(env):
    assistant, backend, _, _ = env
    backend.warm_up(assistant)
    import time
    for _ in range(100):
        if FakeClient.seen_options:
            break
        time.sleep(0.02)
    assert len(FakeClient.seen_options) == 1 and FakeClient.prompts == []
    FakeClient.script = [result("быстро")]
    assert assistant.handle("расскажи подробно что угодно").text == "быстро"
    assert len(FakeClient.seen_options) == 1  # первый ход использовал заранее запущенный клиент


def test_hybrid_backend_falls_back_to_ollama_if_sdk_missing(config, monkeypatch):
    monkeypatch.setattr(ClaudeAgentBackend, "available", staticmethod(lambda: (False, "not installed")))
    config.set("llm.provider", "hybrid")
    assert make_agent_backend(config) is None


def test_hybrid_backend_uses_claude_when_sdk_is_available(config, monkeypatch):
    monkeypatch.setattr(ClaudeAgentBackend, "available", staticmethod(lambda: (True, "ready")))
    config.set("llm.provider", "hybrid")
    assert isinstance(make_agent_backend(config), ClaudeAgentBackend)
