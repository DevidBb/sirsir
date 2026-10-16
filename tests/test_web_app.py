"""Сервер HUD: защита Origin/Host, WebSocket-диалог, отмена, память, настройки."""

import pytest

pytest.importorskip("starlette")
pytest.importorskip("httpx")
from starlette.testclient import TestClient  # noqa: E402

from conftest import FakeLLM, text  # noqa: E402
from jarvis.main import App  # noqa: E402
from jarvis.web_app import Hub, Runtime, _same_origin, create_app  # noqa: E402

BASE = "http://127.0.0.1:8765"
WS = "ws://127.0.0.1:8765/ws"
ORIGIN = {"origin": "http://127.0.0.1:8765"}


@pytest.fixture
def server(config):
    config.set("browser.enabled", False)
    app = App(config, voice=False)
    app.llm = app.assistant.llm = FakeLLM([text("Привет! Я Джарвис.")])
    runtime = Runtime(app, Hub())
    client = TestClient(create_app(runtime, 8765), base_url=BASE)
    with client:
        yield client, app, runtime
    runtime.stop()
    app.close()


def receive_until(ws, kind, limit=50):
    events = []
    for _ in range(limit):
        ev = ws.receive_json()
        events.append(ev)
        if ev["type"] == kind:
            return ev, events
    raise AssertionError(f"нет события {kind}: {[e['type'] for e in events]}")


@pytest.mark.parametrize("host,origin,ok", [
    ("127.0.0.1:8765", None, True),
    ("127.0.0.1:8765", "http://127.0.0.1:8765", True),
    ("localhost:8765", "http://localhost:8765", True),
    ("127.0.0.1:8765", "http://evil.example", False),  # чужой сайт в браузере пользователя
    ("evil.example:8765", None, False),                 # DNS rebinding
    ("127.0.0.1:8765", "http://127.0.0.1:9999", False),
])
def test_same_origin(host, origin, ok):
    headers = {"host": host}
    if origin:
        headers["origin"] = origin
    assert _same_origin(headers, 8765) is ok


def test_index_and_forbidden_origin(server):
    client, _, _ = server
    assert client.get("/").status_code == 200
    assert "J.A.R.V.I.S." in client.get("/").text
    assert client.get("/api/status", headers={"origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/settings", json={"provider": "claude"},
                       headers={"origin": "http://evil.example"}).status_code == 403


def test_websocket_rejects_foreign_origin(server):
    client, _, _ = server
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(WS, headers={"origin": "http://evil.example"}) as ws:
            ws.receive_json()


def test_chat_over_websocket(server):
    client, _, _ = server
    with client.websocket_connect(WS, headers=ORIGIN) as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "message", "text": "Расскажи о себе"})
        reply, events = receive_until(ws, "reply")
        types = [e["type"] for e in events]
        assert "task" in types and "state" in types
        assert reply["text"] == "Привет! Я Джарвис." and reply["ok"]
        assert "".join(e["text"] for e in events if e["type"] == "delta").strip() == "Привет! Я Джарвис."


def test_quick_command_over_websocket(server):
    client, _, _ = server
    with client.websocket_connect(WS, headers=ORIGIN) as ws:
        ws.receive_json()
        ws.send_json({"type": "message", "text": "сколько будет два плюс два"})
        reply, events = receive_until(ws, "reply")
        assert "4" in reply["text"]
        assert [e["name"] for e in events if e["type"] == "tool_start"] == ["calculate"]


def test_ui_cannot_call_arbitrary_tools(server):
    client, _, _ = server
    with client.websocket_connect(WS, headers=ORIGIN) as ws:
        ws.receive_json()
        ws.send_json({"type": "tool", "name": "shutdown_computer", "args": {}})
        ev, _ = receive_until(ws, "notice")
        assert "нельзя вызвать" in ev["text"]


def test_cancel_sets_stop(server):
    client, app, runtime = server
    with client.websocket_connect(WS, headers=ORIGIN) as ws:
        ws.receive_json()
        ws.send_json({"type": "cancel"})
        ev, _ = receive_until(ws, "cancelled")
        assert app.assistant.emergency.active


def test_confirm_button_flow(server):
    client, app, _ = server
    with client.websocket_connect(WS, headers=ORIGIN) as ws:
        ws.receive_json()
        ws.send_json({"type": "message", "text": "очисти память"})
        reply, _ = receive_until(ws, "reply")
        assert reply["pending_confirmation"] is True
        ws.send_json({"type": "confirm", "answer": False})
        reply, _ = receive_until(ws, "reply")
        assert "отменено" in reply["text"].lower()


def test_memory_crud_and_secret_rejection(server):
    client, app, _ = server
    assert client.post("/api/memory/facts", json={"text": "Люблю короткие ответы"}).json()["ok"]
    secret = client.post("/api/memory/facts", json={"text": "мой пароль от почты qwerty123"}).json()
    assert not secret["ok"] and "секрет" in secret["error"]
    facts = client.get("/api/memory").json()["facts"]
    assert [f["text"] for f in facts] == ["Люблю короткие ответы"]
    fid = facts[0]["id"]
    assert client.put(f"/api/memory/facts/{fid}", json={"text": "Люблю подробные ответы"}).json()["ok"]
    assert not client.put(f"/api/memory/facts/{fid}", json={"text": "token ghp_aaaaaaaaaaaaaaaaaaaaaaaa"}).json()["ok"]
    assert app.memory.facts() == ["Люблю подробные ответы"]
    assert client.delete(f"/api/memory/facts/{fid}").json()["ok"]
    assert app.memory.facts() == []


def test_switch_provider(server):
    client, app, _ = server
    assert client.post("/api/settings", json={"provider": "claude"}).json()["ok"]
    assert app.assistant.agent_backend is not None
    assert app.memory.get_setting("llm.provider") == "claude"
    assert client.post("/api/settings", json={"provider": "gpt"}).status_code == 400
    client.post("/api/settings", json={"provider": "ollama"})
    assert app.assistant.agent_backend is None


def test_status_lists_tools_and_checks(server):
    client, _, _ = server
    s = client.get("/api/status").json()
    assert s["state"] == "idle" and s["provider"]["active"] == "ollama"
    assert any(t["name"] == "calculate" for t in s["tools"])
    assert any(c["name"] == "Python" for c in s["checks"])
    assert s["mcp"] == []  # в config.toml серверов нет


def test_screenshot_path_traversal(server):
    client, _, _ = server
    assert client.get("/api/screenshots/..%2F..%2Fconfig.toml").status_code == 404


def test_memory_search_kinds_and_tasks(server):
    client, app, _ = server
    client.post("/api/memory/facts", json={"text": "Отвечай коротко", "kind": "preference"})
    tid = app.memory.add_task("найди видео про котов", "нашёл 5 видео", ["youtube_search"])
    found = client.get("/api/memory", params={"q": "коты видео"}).json()["found"]
    assert found[0]["kind"] == "task"
    data = client.get("/api/memory").json()
    assert data["facts"][0]["kind"] == "preference" and data["tasks"][0]["id"] == tid
    fid = data["facts"][0]["id"]
    assert client.put(f"/api/memory/facts/{fid}", json={"text": "Отвечай коротко", "kind": "fact"}).json()["ok"]
    assert app.memory.list_facts()[0]["kind"] == "fact"
    assert client.delete(f"/api/memory/tasks/{tid}").json()["ok"] and app.memory.list_tasks() == []


def test_schedules_api_and_run_now(server):
    client, app, _ = server
    app.llm = app.assistant.llm = FakeLLM([text("Сводка: всё спокойно.")])
    bad = client.post("/api/schedules", json={"prompt": "x", "every_minutes": 3})
    assert bad.status_code == 400 and "15" in bad.json()["error"]
    created = client.post("/api/schedules", json={"prompt": "сводка новостей", "time": "09:00", "days": "weekdays"})
    sid = created.json()["id"]
    assert created.json()["items"][0]["when"] == "по будням в 09:00"
    with client.websocket_connect(WS, headers=ORIGIN) as ws:
        ws.receive_json()
        assert client.post(f"/api/schedules/{sid}/run").json()["ok"]
        task, _ = receive_until(ws, "task")
        assert task["text"] == "⏰ сводка новостей"
        reply, _ = receive_until(ws, "reply")
        assert reply["text"] == "Сводка: всё спокойно." and reply["scheduled"]
    runs = client.get("/api/schedules").json()["runs"]
    assert runs[0]["status"] == "ok" and runs[0]["schedule_id"] == sid
    assert client.put(f"/api/schedules/{sid}", json={"enabled": False}).json()["items"][0]["enabled"] is False
    assert client.delete(f"/api/schedules/{sid}").json()["items"] == []
    assert client.delete(f"/api/schedules/{sid}").status_code == 404
