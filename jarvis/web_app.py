"""Веб-интерфейс (HUD): локальный сервер Starlette + WebSocket.

    python -m jarvis --web

Безопасность:
* сервер слушает только 127.0.0.1 (другие адреса отклоняются при запуске);
* WebSocket и изменяющие запросы принимаются только со страницы самого HUD (проверка
  Origin и Host) — чужой сайт в браузере не сможет управлять Джарвисом;
* интерфейс не выполняет инструменты в обход помощника: кнопки «открыть результат»
  и «подтвердить» проходят те же проверки, что и команды голосом.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from .assistant import EXIT, Reply
from .config import Config

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "web"
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
# Инструменты, которые кнопки интерфейса могут вызвать напрямую (без модели).
UI_TOOLS = {"open_result", "browser_tabs", "browser_switch_tab", "browser_screenshot", "read_page",
            "browser_back"}
FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><circle cx="16" cy="16" r="12" '
           'fill="none" stroke="#54c8ff" stroke-width="3"/><circle cx="16" cy="16" r="5" fill="#54c8ff"/></svg>')
IDLE, THINKING, ACTING, ERROR = "idle", "thinking", "acting", "error"


class Hub:
    """Рассылка событий всем вкладкам HUD из любых потоков."""

    def __init__(self, history: int = 300):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[asyncio.Queue] = set()
        self.history: deque[dict] = deque(maxlen=history)
        self._lock = threading.Lock()

    def publish(self, event: dict) -> None:
        event = {**event, "ts": time.time()}
        with self._lock:
            if event.get("type") not in ("delta",):
                self.history.append(event)
            clients = list(self.clients)
        if self.loop is None:
            return
        for q in clients:
            self.loop.call_soon_threadsafe(q.put_nowait, event)


class Runtime:
    """Связывает App (ядро помощника) с интерфейсом: очередь команд, состояние, отмена."""

    def __init__(self, app: Any, hub: Hub):
        self.app = app
        self.hub = hub
        self.state = IDLE
        self.current: str | None = None
        self.inbox: queue.Queue[tuple[str, Any] | None] = queue.Queue()
        self.task_id = 0
        app.assistant.on_event = self._on_assistant_event
        app.observer = self._on_voice_event
        self.voice_task: int | None = None
        self._thread = threading.Thread(target=self._worker, name="assistant", daemon=True)
        self._thread.start()

    def set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        self.hub.publish({"type": "state", "state": state, "detail": detail})

    def _on_assistant_event(self, event: dict) -> None:
        event = {**event, "task": self.task_id}
        if event["type"] == "tool_start":
            self.set_state(ACTING, event.get("name", ""))
        elif event["type"] == "tool_end":
            self.hub.publish(event)
            self.set_state(THINKING)
            return
        self.hub.publish(event)

    # --- локальный голос (голосовой цикл App в своём потоке) ---
    VOICE_STATES = {"idle": IDLE, "listening": "listening", "speaking": "speaking",
                    "muted": IDLE, "off": IDLE}

    def _on_voice_event(self, event: dict) -> None:
        kind = event["type"]
        if kind == "voice_state":
            state = event["state"]
            if self.voice_task is None or state in ("listening", "speaking"):
                detail = {"idle": "жду «Джарвис»", "muted": "микрофон выключен"}.get(state, "")
                self.set_state(self.VOICE_STATES.get(state, IDLE), detail)
            self.hub.publish({"type": "voice", **self.app.voice_status()})
        elif kind == "voice_heard":
            self.task_id += 1
            self.voice_task = self.task_id
            self.hub.publish({"type": "task", "task": self.task_id, "text": event["text"],
                              "source": "voice"})
            self.set_state(THINKING)
        elif kind == "voice_reply":
            task = self.voice_task if self.voice_task is not None else self.task_id
            self.voice_task = None
            self._publish_reply(task, event.get("reply"))
        else:
            self.hub.publish(event)

    def voice_command(self, kind: str, value: Any = None) -> None:
        app = self.app
        if not app.voice_active:
            self.hub.publish({"type": "notice", "text": "Локальный голос не запущен "
                              "(запустите run_web.bat или python -m jarvis --web --voice)."})
            return
        if kind == "mic":
            app.set_mic(bool(value))
        elif kind == "listen":
            app.listen_now()
        elif kind == "shut_up":
            app.barged_in = True
            app.speaker.interrupt()
        self.hub.publish({"type": "voice", **app.voice_status()})

    def submit(self, text: str) -> int:
        self.task_id += 1
        self.inbox.put(("message", (self.task_id, text)))
        return self.task_id

    def call_tool(self, name: str, args: dict) -> None:
        if name not in UI_TOOLS:
            raise PermissionError(f"Инструмент {name} нельзя вызвать из интерфейса")
        self.task_id += 1
        self.inbox.put(("tool", (self.task_id, name, args)))

    def cancel(self) -> None:
        """Остановить текущую задачу и очистить очередь (потокобезопасно)."""
        dropped = 0
        while True:
            try:
                self.inbox.get_nowait()
                dropped += 1
            except queue.Empty:
                break
        if self.app.voice_active:
            self.app.emergency_stop()  # ещё и обрывает речь
        else:
            self.app.assistant.emergency_stop()
        self.hub.publish({"type": "cancelled", "task": self.task_id, "dropped": dropped})

    def stop(self) -> None:
        self.inbox.put(None)

    def _worker(self) -> None:
        while True:
            job = self.inbox.get()
            if job is None:
                return
            kind, payload = job
            try:
                if kind == "message":
                    self._handle_message(*payload)
                else:
                    self._handle_tool(*payload)
            except Exception as e:  # noqa: BLE001 — сообщаем, а не молча падаем
                log.exception("Ошибка обработки")
                self.hub.publish({"type": "reply", "task": payload[0], "ok": False,
                                  "text": f"Внутренняя ошибка: {e}"})
                self.set_state(ERROR, str(e))
                continue
            self.set_state(IDLE)

    def _handle_message(self, task: int, text: str) -> None:
        assistant = self.app.assistant
        assistant.emergency.reset()
        self.current = text
        self.hub.publish({"type": "task", "task": task, "text": text})
        self.set_state(THINKING)
        streaming = assistant.agent_backend is None  # Claude шлёт delta сам

        def on_sentence(sentence: str) -> bool:
            if streaming:
                self.hub.publish({"type": "delta", "task": task, "text": sentence + " "})
            return not assistant.emergency.active

        with self.app.handle_lock:  # голосовая команда в это время ждёт
            reply = assistant.handle(text, on_sentence=on_sentence)
        self.current = None
        self._publish_reply(task, reply)
        if reply is not None and reply.action == EXIT:
            self.hub.publish({"type": "notice", "text": "Команда завершения работы: закройте сервер "
                                                        "(Ctrl+C в терминале)."})

    def _handle_tool(self, task: int, name: str, args: dict) -> None:
        self.set_state(ACTING, name)
        with self.app.handle_lock:
            outcome = self.app.assistant._run_tool(name, args)
        reply = outcome if isinstance(outcome, Reply) else Reply(str(outcome),
                                                                 data=getattr(outcome, "data", None))
        self._publish_reply(task, reply)

    def _publish_reply(self, task: int, reply: Reply | None) -> None:
        if reply is None:
            self.hub.publish({"type": "reply", "task": task, "ok": True, "text": "",
                              "ignored": True})
            return
        self.hub.publish({"type": "reply", "task": task, "ok": True, "text": reply.text,
                          "action": reply.action, "data": reply.data,
                          "pending_confirmation": self.app.assistant.confirmations.has_pending()})

    # --- данные для панелей ---
    def status(self) -> dict[str, Any]:
        from . import checks
        from .providers import provider_status
        app = self.app
        llm_ok = None
        try:
            llm_ok = app.llm.has_model() if app.assistant.agent_backend is None else None
        except Exception:  # noqa: BLE001
            llm_ok = False
        return {
            "state": self.state, "current": self.current, "voice": app.voice_status(),
            "provider": provider_status(app.config), "ollama_ready": llm_ok,
            "browser": app.browser.status() if app.browser else {"running": False,
                                                                 "last_error": "Playwright не установлен"},
            "tools": app.registry.describe(),
            "mcp": app.mcp.describe() if getattr(app, "mcp", None) else [],
            "checks": [{"name": r.name, "ok": r.ok, "detail": r.detail, "required": r.required}
                       for r in checks.run_all(app.config, voice=False)],
        }


def _same_origin(headers, port: int) -> bool:
    """Запрос пришёл со страницы HUD (а не с чужого сайта)."""
    host = (headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
    if host not in LOCAL_HOSTS:
        return False  # защита от DNS rebinding
    origin = headers.get("origin")
    if origin is None:
        return True  # не браузерный клиент (curl, тесты) — Host уже проверен
    allowed = {f"http://{h}:{port}" for h in ("127.0.0.1", "localhost", "[::1]")}
    return origin in allowed


def create_app(runtime: Runtime, port: int):
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import FileResponse, JSONResponse, Response
    from starlette.routing import Mount, Route, WebSocketRoute
    from starlette.staticfiles import StaticFiles
    from starlette.websockets import WebSocket, WebSocketDisconnect

    app_core = runtime.app
    hub = runtime.hub

    def guard(request: Request) -> Response | None:
        if not _same_origin(request.headers, port):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return None

    async def index(request: Request):
        if (resp := guard(request)) is not None:
            return resp
        return FileResponse(STATIC_DIR / "index.html")

    async def favicon(request: Request):
        return Response(FAVICON, media_type="image/svg+xml")

    async def status(request: Request):
        if (resp := guard(request)) is not None:
            return resp
        return JSONResponse(await asyncio.to_thread(runtime.status))

    async def memory(request: Request):
        if (resp := guard(request)) is not None:
            return resp
        mem = app_core.memory
        notes = [{"id": r[0], "text": r[1], "created_at": r[2]} for r in
                 mem.execute("SELECT id, text, created_at FROM notes ORDER BY id DESC LIMIT 100")]
        reminders = [{"id": r[0], "text": r[1], "kind": r[2], "due_at": r[3]} for r in
                     mem.execute("SELECT id, text, kind, due_at FROM reminders WHERE done = 0 "
                                 "ORDER BY due_at")]
        q = request.query_params.get("q", "").strip()
        if q:
            return JSONResponse({"query": q, "found": mem.search(q, 30)})
        return JSONResponse({"facts": mem.list_facts(), "notes": notes, "reminders": reminders,
                             "tasks": mem.list_tasks(),
                             "settings": mem.all_settings(), "actions": _tail_actions(app_core.config)})

    async def memory_item(request: Request):
        if (resp := guard(request)) is not None:
            return resp
        kind = request.path_params["kind"]
        item_id = int(request.path_params["item_id"])
        mem = app_core.memory
        if request.method == "DELETE":
            if kind == "facts":
                ok = mem.delete_fact(item_id)
            elif kind == "notes":
                ok = mem.modify("DELETE FROM notes WHERE id = ?", (item_id,)) > 0
            elif kind == "tasks":
                ok = mem.modify("DELETE FROM task_log WHERE id = ?", (item_id,)) > 0
            elif kind == "reminders":
                ok = mem.modify("UPDATE reminders SET done = 1 WHERE id = ?", (item_id,)) > 0
            else:
                return JSONResponse({"error": "unknown kind"}, status_code=404)
            return JSONResponse({"ok": ok})
        body = await request.json()
        if kind != "facts":
            return JSONResponse({"error": "только факты можно редактировать"}, status_code=400)
        ok = mem.update_fact(item_id, str(body.get("text", "")), body.get("kind"))
        return JSONResponse({"ok": ok, "error": None if ok else
                             "пусто или похоже на секрет (пароль, токен) — не сохраняю"})

    async def add_fact(request: Request):
        if (resp := guard(request)) is not None:
            return resp
        body = await request.json()
        ok = app_core.memory.add_fact(str(body.get("text", "")), str(body.get("kind", "fact")))
        return JSONResponse({"ok": ok, "error": None if ok else
                             "пусто, уже есть или похоже на секрет — не сохраняю"})

    async def settings(request: Request):
        if (resp := guard(request)) is not None:
            return resp
        body = await request.json()
        if "provider" in body:
            try:
                app_core.set_provider(str(body["provider"]))
            except ValueError as e:
                return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        hub.publish({"type": "notice", "text": f"Модель: {app_core.config.get('llm.provider')}"})
        return JSONResponse({"ok": True})

    # --- звук: микрофоны, уровень, проверки ---
    async def audio(request: Request):
        if (resp := guard(request)) is not None:
            return resp
        from .audio_diag import list_microphones
        info = await asyncio.to_thread(list_microphones, str(app_core.config.get("audio.input_device", "") or ""))
        info["voice"] = app_core.voice_status()
        info["speaker"] = app_core.speaker.describe() if app_core.voice_active else ""
        return JSONResponse(info)

    async def audio_level(request: Request):
        if (resp := guard(request)) is not None:
            return resp
        mic = app_core.mic if app_core.voice_active else None
        return JSONResponse({"active": mic is not None, "muted": bool(mic and mic.muted),
                             "level": round(float(getattr(mic, "level", 0.0)), 4) if mic else 0.0})

    async def audio_device(request: Request):
        if (resp := guard(request)) is not None:
            return resp
        spec = str((await request.json()).get("spec", ""))[:300]
        if app_core.voice_active and app_core.mic is not None:
            try:
                await asyncio.to_thread(app_core.mic.switch, spec or None)
            except Exception as e:  # noqa: BLE001 — устройство занято, отключено
                return JSONResponse({"ok": False, "error": f"не удалось открыть микрофон: {e}"})
        app_core.config.set("audio.input_device", spec)
        app_core.memory.set_setting("audio.input_device", spec)
        name = app_core.mic.name if app_core.voice_active and app_core.mic else spec.split("|")[0]
        hub.publish({"type": "voice", **app_core.voice_status()})
        return JSONResponse({"ok": True, "name": name or "по умолчанию",
                             "applied": app_core.voice_active})

    async def audio_test(request: Request):
        if (resp := guard(request)) is not None:
            return resp
        if app_core.voice_active:
            return JSONResponse({"ok": False, "error": "Микрофон сейчас слушает Джарвис — смотрите "
                                 "индикатор уровня и скажите что-нибудь."})
        spec = str((await request.json()).get("spec", ""))[:300]
        from .audio_diag import test_microphone
        try:
            result = await asyncio.to_thread(test_microphone, spec or None)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, **result})

    async def audio_speak(request: Request):
        if (resp := guard(request)) is not None:
            return resp
        if not app_core.voice_active:
            return JSONResponse({"ok": False, "error": "Голос не запущен (run_web.bat или --web --voice)."})

        def speak():
            with app_core.handle_lock:
                app_core._speak(app_core.mic, "Проверка голоса. Если вы меня слышите, всё в порядке.")
        threading.Thread(target=speak, daemon=True, name="tts-test").start()
        return JSONResponse({"ok": True, "speaker": app_core.speaker.describe()})

    async def screenshot(request: Request):
        if (resp := guard(request)) is not None:
            return resp
        name = Path(request.path_params["name"]).name  # без ../
        path = app_core.config.root / "data" / "screenshots" / name
        if not name.endswith(".png") or not path.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(path)

    async def ws_endpoint(ws: WebSocket):
        if not _same_origin(ws.headers, port):
            await ws.close(code=1008)
            return
        await ws.accept()
        q: asyncio.Queue = asyncio.Queue()
        with hub._lock:
            hub.clients.add(q)
            history = list(hub.history)
        await ws.send_json({"type": "hello", "state": runtime.state, "history": history,
                            "provider": app_core.config.get("llm.provider", "ollama"),
                            "voice": app_core.voice_status()})

        async def sender():
            while True:
                await ws.send_json(await q.get())
        send_task = asyncio.create_task(sender())
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                kind = msg.get("type")
                if kind == "message" and str(msg.get("text", "")).strip():
                    runtime.submit(str(msg["text"])[:4000])
                elif kind == "cancel":
                    runtime.cancel()
                elif kind == "confirm":
                    phrase = (app_core.config.get("security.confirm_phrases", ["подтверждаю"])[0]
                              if msg.get("answer") else
                              app_core.config.get("security.cancel_phrases", ["отмена"])[0])
                    runtime.submit(phrase)
                elif kind in ("mic", "listen", "shut_up"):
                    runtime.voice_command(kind, msg.get("on"))
                elif kind == "tool":
                    try:
                        runtime.call_tool(str(msg.get("name")), dict(msg.get("args") or {}))
                    except PermissionError as e:
                        await ws.send_json({"type": "notice", "text": str(e)})
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            send_task.cancel()
            with hub._lock:
                hub.clients.discard(q)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):
        hub.loop = asyncio.get_running_loop()
        yield

    routes = [
        Route("/", index),
        Route("/favicon.ico", favicon),
        Route("/api/status", status),
        Route("/api/memory", memory),
        Route("/api/memory/facts", add_fact, methods=["POST"]),
        Route("/api/memory/{kind}/{item_id:int}", memory_item, methods=["DELETE", "PUT"]),
        Route("/api/settings", settings, methods=["POST"]),
        Route("/api/screenshots/{name}", screenshot),
        Route("/api/audio", audio),
        Route("/api/audio/level", audio_level),
        Route("/api/audio/device", audio_device, methods=["POST"]),
        Route("/api/audio/test", audio_test, methods=["POST"]),
        Route("/api/audio/speak", audio_speak, methods=["POST"]),
        WebSocketRoute("/ws", ws_endpoint),
        Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
    ]
    return Starlette(routes=routes, lifespan=lifespan)


def _tail_actions(config: Config, limit: int = 60) -> list[str]:
    path = config.path("security.audit_log")
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-limit:][::-1]


def start_voice(core: Any, hub: Hub) -> None:
    """Загрузка моделей речи и голосовой цикл в фоне; при ошибке HUD работает без голоса."""
    def worker() -> None:
        hub.publish({"type": "notice", "text": "Загружаю модели речи…"})
        try:
            parts = core.build_voice()
        except Exception as e:  # noqa: BLE001 — нет моделей, микрофона, библиотек
            log.exception("Голос не запущен")
            if isinstance(e, ModuleNotFoundError):
                core.voice_error = f"не установлен пакет {e.name} (pip install -r requirements.txt)"
            else:
                core.voice_error = str(e).splitlines()[0] if str(e) else type(e).__name__
            hub.publish({"type": "notice", "text": f"Локальный голос недоступен: {core.voice_error}. "
                                                    "Чат работает; подробности — «Диагностика»."})
            core.reminders.start(lambda text: hub.publish({"type": "notice", "text": text,
                                                           "reminder": True}))
            return
        hub.publish({"type": "notice", "text": f"Голос готов: скажите «Джарвис» или нажмите «Слушать». "
                                                f"Микрофон: {parts.mic.name}. Звук обрабатывается "
                                                f"на этом компьютере."})
        core.run_voice(parts, console=False)
    threading.Thread(target=worker, name="voice", daemon=True).start()


def run(config: Config, app_factory, open_browser: bool | None = None, voice: bool = False) -> int:
    import uvicorn
    host = str(config.get("web.host", "127.0.0.1"))
    port = int(config.get("web.port", 8765))
    if host not in LOCAL_HOSTS:
        print(f"web.host = {host}: удалённый доступ не поддерживается (нет аутентификации). "
              f"Использую 127.0.0.1.")
        host = "127.0.0.1"
    core = app_factory()
    hub = Hub()
    runtime = Runtime(core, hub)
    if voice:
        start_voice(core, hub)  # напоминания запустит голосовой цикл (или start_voice при ошибке)
    else:
        core.reminders.start(lambda text: hub.publish({"type": "notice", "text": text,
                                                       "reminder": True}))
    web = create_app(runtime, port)
    url = f"http://127.0.0.1:{port}/"
    print(f"JARVIS HUD: {url}  (Ctrl+C — выход)")
    if open_browser if open_browser is not None else config.get("web.open_browser", True):
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run(web, host=host, port=port, log_level="warning")
    finally:
        runtime.stop()
        core.quit()
        core.close()
    return 0
