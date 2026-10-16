"""Провайдер Claude Agent SDK с подставным клиентом (без сети и без авторизации)."""

import asyncio

import pytest

pytest.importorskip("claude_agent_sdk")
import claude_agent_sdk  # noqa: E402
from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, TextBlock  # noqa: E402

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
    hang = False

    def __init__(self, options):
        self.options = options
        FakeClient.seen_options.append(options)
        self.interrupted = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def query(self, prompt):
        self.prompt = prompt

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
    return assistant, backend, calls, events


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


def test_make_agent_backend_by_config(config):
    assert make_agent_backend(config) is None  # по умолчанию — Ollama
    config.set("llm.provider", "claude")
    config.set("llm.claude.model", "haiku")
    backend = make_agent_backend(config)
    assert isinstance(backend, ClaudeAgentBackend) and backend.model == "haiku"
