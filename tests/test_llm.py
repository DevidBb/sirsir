import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from jarvis.llm import Conversation, LLMError, OllamaClient, strip_thinking


def test_strip_thinking():
    assert strip_thinking("<think>хм…</think> **Привет**, мир") == "Привет, мир"
    assert strip_thinking("ответ <think>незакрыто") == "ответ"


def test_rejects_remote_host():
    with pytest.raises(LLMError):
        OllamaClient("http://192.168.1.5:11434", "qwen3:8b")


def test_conversation_limit():
    c = Conversation(max_turns=2)
    for i in range(3):
        c.begin_turn(f"q{i}")
        c.add({"role": "assistant", "content": f"a{i}"})
        c.end_turn()
    msgs = c.messages("sys")
    assert [m["content"] for m in msgs] == ["sys", "q1", "a1", "q2", "a2"]
    c.begin_turn("q3")
    c.abort_turn()
    assert len(c) == 2


@pytest.fixture
def fake_ollama():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj):
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send({"models": [{"name": "qwen3:8b"}]})

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append(data)
            self._send({"message": {"role": "assistant", "content": "<think>x</think>Привет!"},
                        "prompt_eval_count": 1234, "eval_count": 7})

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", received
    server.shutdown()


def test_client_against_fake_server(fake_ollama):
    url, received = fake_ollama
    client = OllamaClient(url, "qwen3:8b", num_predict=50)
    assert client.has_model()
    msg = client.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])
    assert msg["content"] == "Привет!"
    req = received[0]
    assert req["think"] is False and req["stream"] is False
    assert req["options"]["num_predict"] == 50
    assert req["tools"] == [{"type": "function"}]
    assert client.last_stats["prompt_eval_count"] == 1234 and client.last_stats["eval_count"] == 7


def test_client_unavailable():
    client = OllamaClient("http://127.0.0.1:9", "qwen3:8b", timeout=2)
    with pytest.raises(LLMError):
        client.chat([{"role": "user", "content": "hi"}])
