import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from jarvis.gemini import GeminiClient, make_llm, to_openai
from jarvis.llm import LLMError, OllamaClient


@pytest.fixture
def fake_gemini():
    """Подставной OpenAI-совместимый сервер: отвечает по очереди из списка (код, тело или SSE)."""
    state = {"responses": [], "received": [], "headers": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            state["received"].append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            state["headers"].append(dict(self.headers))
            code, body = state["responses"].pop(0)
            self.send_response(code)
            if isinstance(body, list):  # поток
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in body:
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                return
            data = json.dumps(body).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["client"] = GeminiClient(key="test-key", retry_pause=0,
                                   url=f"http://127.0.0.1:{server.server_port}/chat")
    yield state
    server.shutdown()


def reply(message):
    return 200, {"choices": [{"message": message}], "usage": {"prompt_tokens": 10, "completion_tokens": 3}}


def test_text_answer_and_request(fake_gemini):
    fake_gemini["responses"].append(reply({"role": "assistant", "content": "**Привет**!"}))
    msg = fake_gemini["client"].chat([{"role": "user", "content": "hi"}], tools=[
        {"type": "function", "function": {"name": "set_timer", "parameters": {}}}])
    assert msg["content"] == "Привет!"
    req = fake_gemini["received"][0]
    assert req["model"] == "gemini-3.6-flash" and req["reasoning_effort"] == "low"
    assert req["stream"] is False and req["tools"][0]["function"]["name"] == "set_timer"
    assert fake_gemini["headers"][0]["Authorization"] == "Bearer test-key"
    assert fake_gemini["client"].last_stats == {"prompt_eval_count": 10, "eval_count": 3}


def test_tool_call_keeps_id_and_thought_signature(fake_gemini):
    sig = {"google": {"thought_signature": "abc"}}
    fake_gemini["responses"].append(reply({"role": "assistant", "tool_calls": [
        {"id": "c1", "type": "function", "extra_content": sig,
         "function": {"name": "set_timer", "arguments": '{"minutes": 5}'}}]}))
    msg = fake_gemini["client"].chat([{"role": "user", "content": "таймер"}])
    call = msg["tool_calls"][0]
    assert call["function"] == {"name": "set_timer", "arguments": {"minutes": 5}}
    assert call["id"] == "c1" and call["extra_content"] == sig


def test_history_conversion_matches_tool_results_to_calls():
    sig = {"google": {"thought_signature": "abc"}}
    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "extra_content": sig, "function": {"name": "a", "arguments": {"x": 1}}},
            {"id": "c2", "function": {"name": "b", "arguments": {}}}]},
        {"role": "tool", "tool_name": "a", "content": "готово"},
        {"role": "user", "content": "дальше"},
    ]
    out = to_openai(history)
    calls = out[2]["tool_calls"]
    # Второй вызов остался без ответа (ждал подтверждения) — API его не примет, отбрасываем.
    assert [c["id"] for c in calls] == ["c1"]
    assert calls[0]["extra_content"] == sig and calls[0]["function"]["arguments"] == '{"x": 1}'
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "готово"}
    assert out[4] == {"role": "user", "content": "дальше"}


def test_image_becomes_content_part():
    out = to_openai([{"role": "user", "content": "что тут?", "images": ["/9j/AAA"]}])
    parts = out[0]["content"]
    assert parts[0] == {"type": "text", "text": "что тут?"}
    assert parts[1]["image_url"]["url"] == "data:image/jpeg;base64,/9j/AAA"


def test_streaming_text_and_tool_calls(fake_gemini):
    fake_gemini["responses"].append((200, [
        {"choices": [{"delta": {"role": "assistant", "content": "Раз. "}}]},
        {"choices": [{"delta": {"content": "Два."}}]},
    ]))
    pieces = []
    msg = fake_gemini["client"].chat([{"role": "user", "content": "hi"}],
                                     on_text=lambda p: pieces.append(p) or True)
    assert pieces == ["Раз. ", "Два."] and msg["content"] == "Раз. Два."
    assert fake_gemini["received"][0]["stream"] is True

    fake_gemini["responses"].append((200, [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c9", "type": "function",
                                                "function": {"name": "add_note", "arguments": '{"text":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ' "x"}'}}]}}]},
    ]))
    msg = fake_gemini["client"].chat([{"role": "user", "content": "hi"}], on_text=lambda p: True)
    assert msg["tool_calls"][0]["id"] == "c9"
    assert msg["tool_calls"][0]["function"] == {"name": "add_note", "arguments": {"text": "x"}}


def test_errors_are_clear_and_hide_key(fake_gemini):
    client = fake_gemini["client"]
    fake_gemini["responses"].append((429, [{"error": {"message": "Quota exceeded for test-key"}}]))
    with pytest.raises(LLMError, match="лимит") as e:
        client.chat([{"role": "user", "content": "hi"}])
    assert "test-key" not in str(e.value)
    # Временный сбой Google — один повтор.
    fake_gemini["responses"] += [(503, {"error": {"message": "high demand"}}), reply({"content": "ок"})]
    assert client.chat([{"role": "user", "content": "hi"}])["content"] == "ок"


def test_missing_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    client = GeminiClient()
    assert not client.has_model()
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        client.chat([{"role": "user", "content": "hi"}])


def test_make_llm_by_provider(config):
    assert isinstance(make_llm(config), OllamaClient)
    config.set("llm.provider", "gemini")
    llm = make_llm(config)
    assert isinstance(llm, GeminiClient) and llm.model == config.get("llm.gemini.model")


def test_provider_status_shows_gemini(config, monkeypatch):
    from jarvis.providers import provider_status
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    status = provider_status(config)
    assert "gemini" in status["options"] and status["gemini"]["available"]


def test_busy_model_falls_back_to_spare(fake_gemini):
    client = fake_gemini["client"]
    busy = (503, [{"error": {"message": "high demand"}}])
    fake_gemini["responses"] += [busy, busy, reply({"content": "ответ запасной"})]
    assert client.chat([{"role": "user", "content": "hi"}])["content"] == "ответ запасной"
    assert [r["model"] for r in fake_gemini["received"]] == ["gemini-3.6-flash"] * 2 + ["gemini-3.5-flash"]
    # Неверный ключ — не перегрузка: запасную модель не спрашиваем.
    fake_gemini["received"].clear()
    fake_gemini["responses"].append((403, {"error": {"message": "API key not valid"}}))
    with pytest.raises(LLMError, match="ключ"):
        client.chat([{"role": "user", "content": "hi"}])
    assert len(fake_gemini["received"]) == 1
