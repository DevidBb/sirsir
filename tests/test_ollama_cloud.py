"""Контракт Ollama Cloud: настоящий клиент и разбор HTTP/NDJSON без внешней сети."""

import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from jarvis.gemini import make_llm
from jarvis.llm import LLMError, OllamaClient
from jarvis.ollama_cloud import BASE_URL, OllamaCloudClient


@pytest.fixture
def transport(monkeypatch):
    state = {"requests": [], "reply": {"message": {"content": "Ответ облака"}}}

    def open_request(req, timeout=None):
        state["requests"].append(req)
        response = state["reply"]
        if isinstance(response, Exception):
            raise response
        body = response if isinstance(response, bytes) else json.dumps(response).encode()
        return io.BytesIO(body)

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    return state


def test_cloud_auth_payload_and_tool_contract(transport):
    client = OllamaCloudClient(model="chosen-model", key="private-key")
    tools = [{"type": "function", "function": {"name": "set_timer", "parameters": {}}}]
    transport["reply"] = {"message": {"content": "", "tool_calls": [
        {"function": {"name": "set_timer", "arguments": {"minutes": 5}}}]}}
    reply = client.chat([{"role": "user", "content": "таймер"}], tools=tools, fmt="json")
    request = transport["requests"][0]
    payload = json.loads(request.data)
    assert request.full_url == BASE_URL + "/api/chat"
    assert request.get_header("Authorization") == "Bearer private-key"
    assert payload["model"] == "chosen-model" and payload["tools"] == tools and payload["format"] == "json"
    assert not {"keep_alive", "think"}.intersection(payload)
    assert "num_ctx" not in payload["options"]
    assert reply["tool_calls"][0]["function"]["arguments"] == {"minutes": 5}


def test_cloud_stream_and_cancel(transport):
    chunks = [{"message": {"content": "Привет "}}, {"message": {"content": "мир"}}, {"done": True}]
    transport["reply"] = b"\n".join(json.dumps(c).encode() for c in chunks)
    client = OllamaCloudClient(key="k")
    pieces = []
    assert client.chat([], on_text=lambda s: pieces.append(s))["content"] == "Привет мир"
    assert pieces == ["Привет ", "мир"]
    assert client.chat([], on_text=lambda s: False)["content"] == "Привет"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("code", [401, 429, 500])
def test_cloud_errors_never_echo_the_key(transport, code, stream):
    secret = "a-long-private-key"
    body = ("x" * 295 + secret + " denied").encode()
    transport["reply"] = urllib.error.HTTPError(BASE_URL, code, "failure", {}, io.BytesIO(body))
    with pytest.raises(LLMError) as error:
        OllamaCloudClient(key=secret).chat([], on_text=(lambda s: True) if stream else None)
    assert secret not in str(error.value) and "a-long" not in str(error.value)
    assert "Ollama Cloud" in str(error.value)


def test_cloud_missing_key_and_local_auth_isolation(transport, monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    with pytest.raises(LLMError, match="OLLAMA_API_KEY"):
        OllamaCloudClient().chat([])
    assert not transport["requests"]
    monkeypatch.setenv("OLLAMA_API_KEY", "private-key")
    OllamaClient("http://localhost:11434", "local").chat([])
    assert transport["requests"][0].get_header("Authorization") is None
    with pytest.raises(LLMError, match="localhost"):
        OllamaClient("https://ollama.com", "remote")


def test_cloud_selected_as_individual_provider(config):
    config.set("llm.provider", "ollama_cloud")
    config.set("llm.ollama_cloud.model", "configured-model")
    llm = make_llm(config)
    assert isinstance(llm, OllamaCloudClient) and llm.model == "configured-model"
    assert llm.preload() and llm.unload() is None


def test_cloud_stream_api_error_is_reported(transport):
    transport["reply"] = b'{"error":"private-key denied"}\n'
    with pytest.raises(LLMError, match="Ollama Cloud") as error:
        OllamaCloudClient(key="private-key").chat([], on_text=lambda s: True)
    assert "private-key" not in str(error.value)


def test_switching_providers_replaces_the_transport(config, monkeypatch):
    from jarvis.gemini import GeminiClient
    from jarvis.main import App
    app = App.__new__(App)
    app.config = config
    app.memory = SimpleNamespace(set_setting=lambda *args: None)
    app.llm = OllamaClient("http://localhost:11434", "local")
    app.assistant = SimpleNamespace(llm=app.llm, agent_backend=None)
    monkeypatch.setattr(OllamaClient, "unload", lambda self: None)
    for provider, cls in [("ollama_cloud", OllamaCloudClient), ("gemini", GeminiClient),
                          ("team", OllamaClient), ("ollama_cloud", OllamaCloudClient), ("ollama", OllamaClient)]:
        assert app.set_provider(provider) == provider
        assert type(app.llm) is cls and app.assistant.llm is app.llm


def test_provider_status_has_all_members_and_no_secrets(config, monkeypatch):
    from jarvis.providers import provider_status
    monkeypatch.setenv("OLLAMA_API_KEY", "private-cloud-key")
    monkeypatch.setenv("GEMINI_API_KEY", "private-gemini-key")
    status = provider_status(config)
    assert {"team", "codex", "ollama_cloud", "gemini", "ollama", "claude"} <= set(status["options"])
    members = {m["key"]: m for m in status["team"]["members"]}
    assert members["ollama_cloud"]["available"] and members["gemini"]["available"]
    assert "private-cloud-key" not in json.dumps(status) and "private-gemini-key" not in json.dumps(status)
