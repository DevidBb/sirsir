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


def test_sentence_stream_splitting():
    from jarvis.llm import SentenceStream
    out = []
    s = SentenceStream(lambda x: out.append(x))
    for piece in ["При", "вет! Я **Джар", "вис**. Это, т.е. место, где ", "гравитация сильна. Сейчас 12.",
                  "30 и всё"]:
        s.feed(piece)
    s.flush()
    assert out == ["Привет! Я Джарвис.", "Это, т.е. место, где гравитация сильна.", "Сейчас 12.30 и всё"]


@pytest.fixture
def fake_stream_server():
    chunks = [{"message": {"content": "Раз. "}}, {"message": {"content": "Два три четыре пять. "}},
              {"message": {"content": "Шесть."}}, {"message": {"content": ""}, "done": True,
                                                   "prompt_eval_count": 42}]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            for c in chunks:
                self.wfile.write((json.dumps(c) + "\n").encode())
                self.wfile.flush()

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_client_streaming(fake_stream_server):
    client = OllamaClient(fake_stream_server, "qwen3:8b")
    pieces = []
    msg = client.chat([{"role": "user", "content": "hi"}], on_text=lambda p: pieces.append(p) or True)
    assert pieces == ["Раз. ", "Два три четыре пять. ", "Шесть."]
    assert msg["content"] == "Раз. Два три четыре пять. Шесть."
    assert client.last_stats["prompt_eval_count"] == 42
    pieces.clear()
    msg = client.chat([{"role": "user", "content": "hi"}], on_text=lambda p: (pieces.append(p), False)[1])
    assert pieces == ["Раз. "] and msg["content"] == "Раз."


def test_clean_markdown_keeps_underscores():
    from jarvis.llm import clean_markdown
    assert clean_markdown("**Жирный** файл my_file.txt, `код`, 2*3=6 逃离") == \
        "Жирный файл my_file.txt, код, 2*3=6"


@pytest.mark.parametrize("raw,name", [
    ('{"name": "set_timer", "arguments": {"seconds": 60}}', "set_timer"),
    ('<tool_call>\n{"name": "add_note", "arguments": {"text": "x"}}\n</tool_call>', "add_note"),
    ('```json\n{"name": "add_note", "arguments": "{\\"text\\": \\"y\\"}"}\n```', "add_note"),
    ("system_info", "system_info"),
])
def test_text_tool_call_parsed(raw, name):
    from jarvis.llm import parse_text_tool_call
    calls = parse_text_tool_call(raw, {"set_timer", "add_note", "system_info"})
    assert calls and calls[0]["function"]["name"] == name


@pytest.mark.parametrize("raw", ['{"name": "rm_rf", "arguments": {}}', "Привет!", '{"a": 1}', "{сломано"])
def test_text_tool_call_rejected(raw):
    from jarvis.llm import parse_text_tool_call
    assert parse_text_tool_call(raw, {"set_timer"}) is None


def test_stream_does_not_speak_json():
    from jarvis.llm import SentenceStream
    out = []
    s = SentenceStream(lambda x: out.append(x))
    for p in ['{"name": "add_note", ', '"arguments": {"text": "Купить хлеб. Молоко."}}']:
        s.feed(p)
    s.flush()
    assert out == []


def test_client_converts_text_tool_call(fake_ollama):
    url, received = fake_ollama
    client = OllamaClient(url, "qwen3:8b")
    # фейковый сервер отвечает текстом «Привет!» — не вызов, остаётся текстом
    msg = client.chat([{"role": "user", "content": "hi"}],
                      tools=[{"type": "function", "function": {"name": "add_note"}}])
    assert "tool_calls" not in msg and msg["content"] == "Привет!"


def test_preload(fake_ollama):
    url, received = fake_ollama
    client = OllamaClient(url, "qwen3:8b", keep_alive="30m")
    assert client.preload()
    assert received[-1] == {"model": "qwen3:8b", "keep_alive": "30m"}
    assert not OllamaClient("http://127.0.0.1:9", "qwen3:8b", timeout=1).preload()


def test_ensure_ollama_already_running(fake_ollama):
    from jarvis.llm import ensure_ollama
    url, _ = fake_ollama
    started = []
    assert ensure_ollama(OllamaClient(url, "m"), popen=lambda *a, **k: started.append(a)) == "running"
    assert started == []


def test_ensure_ollama_starts_server(monkeypatch, tmp_path):
    from jarvis import llm
    exe = tmp_path / "ollama"
    exe.write_text("")
    monkeypatch.setattr(llm, "find_ollama", lambda: str(exe))
    client = OllamaClient("http://127.0.0.1:9", "m", timeout=1)
    state = {"up": False}

    def fake_list():
        if not state["up"]:
            raise llm.LLMError("down")
        return []

    monkeypatch.setattr(client, "list_models", fake_list)
    calls = []

    def popen(cmd, **kw):
        calls.append(cmd)
        state["up"] = True

    assert llm.ensure_ollama(client, popen=popen, sleep=lambda s: None) == "started"
    assert calls == [[str(exe), "serve"]]


def test_ensure_ollama_missing(monkeypatch):
    from jarvis import llm
    monkeypatch.setattr(llm, "find_ollama", lambda: None)
    assert llm.ensure_ollama(OllamaClient("http://127.0.0.1:9", "m", timeout=1)) == "missing"


def test_find_ollama_windows_default(monkeypatch, tmp_path):
    from jarvis import llm
    exe = tmp_path / "Programs" / "Ollama" / "ollama.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert llm.find_ollama() == str(exe)
