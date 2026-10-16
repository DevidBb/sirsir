"""Сквозная проверка HUD так, как его запускает пользователь (run_web.bat).

Запускает ``python -m jarvis --web --voice --no-open`` отдельным процессом, ждёт сервер и
работает с ним как страница HUD: HTML и скрипты, /api/status, WebSocket-диалог (быстрая
команда без модели), плановая задача, экспорт памяти, защита от чужого Origin. В конце
останавливает процесс и проверяет журнал на необработанные ошибки.

Микрофона и Ollama на машине CI нет — голос и модель должны честно сообщить об ошибке,
а HUD продолжать работать. Код выхода 0 — всё в порядке.

    python scripts/hud_smoke.py [--port 8799] [--keep-data]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAILS: list[str] = []
LOG_RECORD = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")


def check(ok: bool, what: str, detail: str = "") -> bool:
    print(("  ok   " if ok else "  FAIL ") + what + (f" — {detail}" if detail else ""), flush=True)
    if not ok:
        FAILS.append(what)
    return ok


def warn(ok: bool, what: str, detail: str = "") -> bool:
    """Зависит от внешних сайтов (капча, сеть) — не провал проверки, но видно в отчёте."""
    print(("  ok   " if ok else "  WARN ") + what + (f" — {detail}" if detail else ""), flush=True)
    return ok


def http(port: int, path: str, method: str = "GET", body: dict | None = None,
         headers: dict | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def wait_port(port: int, proc: subprocess.Popen, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def ws_dialog(port: int, text: str, timeout: float = 90) -> tuple[dict | None, list[dict]]:
    from websockets.sync.client import connect
    events: list[dict] = []
    with connect(f"ws://127.0.0.1:{port}/ws", origin=f"http://127.0.0.1:{port}", open_timeout=20) as ws:
        hello = json.loads(ws.recv(timeout=20))
        events.append(hello)
        ws.send(json.dumps({"type": "message", "text": text}))
        end = time.time() + timeout
        while time.time() < end:
            ev = json.loads(ws.recv(timeout=max(1, end - time.time())))
            events.append(ev)
            if ev.get("type") == "reply":
                return ev, events
    return None, events


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--keep-data", action="store_true", help="не удалять временную папку данных")
    args = parser.parse_args()
    port = args.port

    # Отдельная копия проекта для данных: memory.db, журналы, профиль браузера — не пользовательские.
    work = Path(tempfile.mkdtemp(prefix="jarvis-hud-smoke-"))
    config = (ROOT / "config.toml").read_text(encoding="utf-8")
    config = config.replace("port = 8765", f"port = {port}")
    # Относительные пути config.toml считаются от его папки: data/ — во временной папке
    # (свои memory.db, журналы, профиль браузера), модели и навыки — из проекта.
    config = config.replace('"models/', f'"{(ROOT / "models").as_posix()}/')
    config = config.replace("extra_dirs = []", f'extra_dirs = ["{(ROOT / "skills").as_posix()}"]')
    cfg_path = work / "config.toml"
    cfg_path.write_text(config, encoding="utf-8")

    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    cmd = [sys.executable, "-m", "jarvis", "--config", str(cfg_path), "--web", "--voice", "--no-open",
           "--skip-check"]
    print("Запуск:", " ".join(cmd), flush=True)
    log_out = open(work / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log_out, stderr=subprocess.STDOUT)
    try:
        if not check(wait_port(port, proc, 120), "сервер HUD запустился", f"порт {port}"):
            return finish(proc, work, log_out, args.keep_data)

        status, page = http(port, "/")
        check(status == 200 and b"<html" in page.lower(), "страница HUD отдаётся")
        for asset in ("/static/hud.js", "/static/hud.css", "/static/markdown.js"):
            status, _ = http(port, asset)
            check(status == 200, f"файл {asset}")

        status, raw = http(port, "/api/status")
        st = json.loads(raw) if status == 200 else {}
        check(status == 200 and st.get("state") in ("idle", "thinking", "acting"), "/api/status")
        names = {t["name"] for t in st.get("tools", [])}
        check({"calculate", "internet_search", "fetch_page", "schedule_task", "use_skill"} <= names,
              "инструменты зарегистрированы", f"{len(names)} шт.")
        check({"research", "project-review", "video-pick"} <= {s["name"] for s in st.get("skills", [])},
              "навыки загружены")

        # Голос: модели речи грузятся в фоне; на CI нет микрофона — должна быть понятная ошибка.
        voice: dict = {}
        end = time.time() + 120
        while time.time() < end:
            voice = json.loads(http(port, "/api/status")[1]).get("voice", {})
            if voice.get("enabled") or voice.get("error"):
                break
            time.sleep(2)
        check(voice.get("enabled") or bool(voice.get("error")), "голос: работает или честно сообщает об ошибке",
              voice.get("error") or voice.get("mic", ""))

        reply, events = ws_dialog(port, "сколько будет 17 умножить на 3")
        check(reply is not None and "51" in reply.get("text", ""), "WebSocket: быстрая команда без модели",
              (reply or {}).get("text", "нет ответа")[:80])
        check(any(e.get("type") == "tool_start" and e.get("name") == "calculate" for e in events),
              "ход задачи: событие инструмента")

        status, _ = http(port, "/api/status", headers={"Origin": "http://evil.example"})
        check(status == 403, "чужой сайт не получает доступ (Origin)")

        status, raw = http(port, "/api/schedules", "POST", {"prompt": "проверка расписания", "time": "23:59"})
        sid = json.loads(raw).get("id") if status == 200 else None
        check(sid is not None, "плановая задача создаётся")
        if sid is not None:
            check(http(port, f"/api/schedules/{sid}", "DELETE")[0] == 200, "плановая задача удаляется")

        status, raw = http(port, "/api/memory/export")
        check(status == 200 and json.loads(raw).get("format") == "jarvis-memory", "экспорт памяти")

        # Настоящий поиск через браузер Джарвиса (Chromium, Playwright) — без модели.
        reply, events = ws_dialog(port, "найди в интернете официальный сайт python", 120)
        data = (reply or {}).get("data") or {}
        warn(data.get("kind") == "web" and bool(data.get("items")), "поиск в интернете через браузер Джарвиса",
             f"{data.get('engine', '')}: {len(data.get('items') or [])} результатов"
             if data.get("items") else (reply or {}).get("text", "нет ответа")[:120])

        # Модель: Ollama на CI нет — ответ должен честно сказать об этом за разумное время.
        reply, _ = ws_dialog(port, "объясни подробно, чем отличается TCP от UDP, и приведи примеры", 150)
        text = (reply or {}).get("text", "")
        check(reply is not None and bool(text), "сложный запрос: ответ модели или честная ошибка", text[:100])
    finally:
        code = finish(proc, work, log_out, args.keep_data)
    return code


def finish(proc: subprocess.Popen, work: Path, log_out, keep: bool) -> int:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(20)
        except subprocess.TimeoutExpired:
            proc.kill()
    log_out.close()
    server_log = (work / "server.log").read_text(encoding="utf-8", errors="replace")
    jarvis_log = work / "data" / "jarvis.log"
    text = jarvis_log.read_text(encoding="utf-8", errors="replace") if jarvis_log.exists() else ""
    print("\n--- вывод сервера (конец) ---\n" + server_log[-3000:], flush=True)
    # Ожидаемые ошибки на CI: нет микрофона («Голос не запущен»), нет Ollama. Остальные трассировки — сбой.
    expected = ("Голос не запущен", "LLM:", "Ollama")
    unexpected = []
    for source in (server_log, text):
        # Трассировка (и вся цепочка «During handling…») относится к последней записи журнала с меткой времени.
        record = ""
        for line in source.splitlines():
            if LOG_RECORD.match(line):
                record = line
            elif line.startswith("Traceback") and not any(e in record for e in expected):
                unexpected.append(record[-200:] or line)
    check(not unexpected, "в журнале нет неожиданных ошибок", "; ".join(unexpected)[:600])
    shutil.copy(work / "server.log", ROOT / "data" / "hud-smoke-server.log") if (ROOT / "data").exists() else None
    if jarvis_log.exists() and (ROOT / "data").exists():
        shutil.copy(jarvis_log, ROOT / "data" / "hud-smoke-jarvis.log")
    if not keep:
        shutil.rmtree(work, ignore_errors=True)
    print("\nИтог:", "всё в порядке" if not FAILS else "ошибки: " + "; ".join(FAILS), flush=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
