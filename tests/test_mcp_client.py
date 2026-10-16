"""Внешние MCP-серверы: белый список, подтверждение, недоверенный ответ, честные ошибки."""

import sys
from pathlib import Path

import pytest

from conftest import FakeLLM
from jarvis import mcp_client
from jarvis.assistant import Assistant, Reply
from jarvis.mcp_client import McpManager, ServerConfig, params_from_schema, parse_servers
from jarvis.memory import Memory
from jarvis.tool_registry import ToolError, ToolRegistry

SERVER = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"


def test_parse_servers_validates_config():
    servers, errors = parse_servers({
        "ok": {"command": "npx", "args": ["-y", "x"], "allowed_tools": ["a"], "env_vars": ["TOKEN"]},
        "no-command": {"args": []},
        "bad name!": {"command": "x"},
    })
    assert [s.name for s in servers] == ["ok"] and servers[0].env_vars == ["TOKEN"]
    assert len(errors) == 2


def test_schema_conversion():
    params = params_from_schema({
        "type": "object", "required": ["path"],
        "properties": {"path": {"type": "string"}, "n": {"type": ["integer", "null"]},
                       "files": {"type": "array", "items": {"type": "string"}},
                       "extra": {"type": "object"}}})
    assert set(params) == {"path", "n", "files"}  # необязательный объект пропущен
    assert params["files"].type == "array" and params["files"].items == "string"
    assert params_from_schema({"properties": {"o": {"type": "object"}}, "required": ["o"]}) is None


def test_registry_validates_arrays_and_numbers():
    from jarvis.tool_registry import Param, Tool
    reg = ToolRegistry()
    reg.register(Tool("t", "", lambda **a: "", {
        "xs": Param("array", "", items="number"), "s": Param("string", "", required=False, multiline=True)}))
    assert reg.validate("t", {"xs": [1, 2.5], "s": "a\nb"})[1] == {"xs": [1, 2.5], "s": "a\nb"}
    with pytest.raises(ToolError):
        reg.validate("t", {"xs": [1, "два"]})
    with pytest.raises(ToolError):
        reg.validate("t", {"xs": [True]})
    with pytest.raises(ToolError):
        reg.validate("t", {"xs": "1,2"})


@pytest.fixture
def real(config, tmp_path):
    """Настоящий процесс MCP-сервера по stdio."""
    pytest.importorskip("mcp")
    registry = ToolRegistry()
    server = ServerConfig("echo", sys.executable, [str(SERVER)],
                          allowed_tools=["echo", "add", "delete_all", "needs_object", "fail", "missing"],
                          auto_tools=["echo", "add", "fail"], timeout=30)
    manager = McpManager([server], registry, tmp_path / "mcp")
    manager.start()
    assert manager.wait_ready(60)
    assistant = Assistant(config, FakeLLM([]), registry, Memory(":memory:"))
    yield manager, registry, assistant
    manager.close()


def test_real_server_whitelist_and_calls(real):
    manager, registry, assistant = real
    status = manager.describe()[0]
    assert status["state"] == "ready", status
    # Только разрешённые и поддерживаемые инструменты.
    assert sorted(registry.names()) == ["mcp_echo_add", "mcp_echo_delete_all", "mcp_echo_echo", "mcp_echo_fail"]
    assert any("missing" in s for s in status["skipped"]) and any("needs_object" in s for s in status["skipped"])

    out = assistant._run_tool("mcp_echo_echo", {"text": "привет", "times": 2})
    assert "привет привет" in out and "<untrusted>" in out and out.data["kind"] == "mcp"
    assert "6.5" in assistant._run_tool("mcp_echo_add", {"numbers": [1, 2.5, 3]})
    # Лишний аргумент отклоняется до сервера.
    assert "Лишние аргументы" in assistant._run_tool("mcp_echo_echo", {"text": "x", "cmd": "rm"})

    failed = assistant._run_tool("mcp_echo_fail", {})
    assert failed.ok is False and "ошибку" in failed


def test_real_server_tool_outside_auto_needs_confirmation(real):
    _, _, assistant = real
    reply = assistant._run_tool("mcp_echo_delete_all", {})
    assert isinstance(reply, Reply) and "delete_all" in reply.text
    done = assistant.handle("подтверждаю")
    assert done.text == "всё удалено"  # пользователю — без служебной обёртки
    assert "<untrusted>" in assistant.conversation.messages("")[-1]["content"]  # модели — с ней


def test_missing_program_and_env_are_reported(config, tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    monkeypatch.delenv("JARVIS_TEST_TOKEN", raising=False)
    registry = ToolRegistry()
    manager = McpManager([
        ServerConfig("nope", "jarvis-no-such-program-xyz", allowed_tools=["a"], timeout=10),
        ServerConfig("token", sys.executable, [str(SERVER)], allowed_tools=["echo"],
                     env_vars=["JARVIS_TEST_TOKEN"]),
    ], registry, tmp_path / "mcp")
    manager.start()
    assert manager.wait_ready(30)
    status = {s["name"]: s for s in manager.describe()}
    assert status["nope"]["state"] == "error" and status["nope"]["error"]
    assert status["token"]["state"] == "error" and "JARVIS_TEST_TOKEN" in status["token"]["error"]
    assert registry.names() == []
    manager.close()


def test_setup_without_servers_returns_none(config):
    assert mcp_client.setup(config, ToolRegistry()) is None


def test_setup_from_config(config):
    pytest.importorskip("mcp")
    config.set("mcp.servers", {"echo": {"command": sys.executable, "args": [str(SERVER)],
                                        "allowed_tools": ["echo"], "auto_tools": ["echo"]},
                               "broken": {"args": []}})
    registry = ToolRegistry()
    manager = mcp_client.setup(config, registry)
    try:
        assert manager.wait_ready(60)
        status = {s["name"]: s for s in manager.describe()}
        assert status["echo"]["state"] == "ready" and registry.names() == ["mcp_echo_echo"]
        assert status["broken"]["state"] == "error" and "command" in status["broken"]["error"]
    finally:
        manager.close()
