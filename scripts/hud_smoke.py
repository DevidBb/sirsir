"""Сквозная проверка HUD так, как его запускает пользователь (run_web.bat).

Запускает ``python -m jarvis.supervisor --web --voice --no-open`` (как run_web.bat) отдельным
процессом, ждёт сервер и работает с ним как страница HUD: HTML и скрипты, /api/status,
WebSocket-диалог (быстрая команда без модели), плановая задача, экспорт памяти, защита от
чужого Origin. В конце — «Обновить и перезапустить»: Джарвис выходит с кодом 75, супервизор
запускает проверку обновления без изменений (JARVIS_UPDATE_DRY_RUN) и снова поднимает HUD.
Затем процессы останавливаются, журнал проверяется на необработанные ошибки.

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

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            check(False, f"порт {port} свободен", "занят — остался процесс от прошлой проверки или другой Джарвис")
            return 1
    except OSError:
        pass

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

    # Обновление по кнопке — только проверка (dry run): смоук-тест не меняет файлы проекта.
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "JARVIS_UPDATE_DRY_RUN": "1"}
    cmd = [sys.executable, "-m", "jarvis.supervisor", "--config", str(cfg_path), "--web", "--voice",
           "--no-open", "--skip-check"]
    print("Запуск:", " ".join(cmd), flush=True)
    log_out = open(work / "server.log", "w", encoding="utf-8")
    # Своя группа процессов: в конце остановим и супервизор, и запущенного им Джарвиса.
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
             else {"start_new_session": True})
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log_out, stderr=subprocess.STDOUT, **group)
    try:
        if not check(wait_port(port, proc, 120), "сервер HUD запустился", f"порт {port}"):
            return finish(proc, work, log_out, args.keep_data)

        status, page = http(port, "/")
        check(status == 200 and b"<html" in page.lower(), "страница HUD отдаётся")
        check(b"/static/hud.js?v=" in page, "скрипты с версией (после обновления не из кэша)")
        for asset in ("/static/hud.js", "/static/hud.css", "/static/markdown.js", "/static/orb.js"):
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
        memory_vault(port)

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

        screenshots(port)
        restart_after_update(port, proc)
    finally:
        code = finish(proc, work, log_out, args.keep_data)
    return code


SMOKE_DECISION = "Смоук-проверка HUD записывает решения"


def memory_vault(port: int) -> None:
    """Заметки Obsidian (во временной папке данных): указатель, решение быстрой командой без модели."""
    status, raw = http(port, "/api/memory/brief")
    vault = (json.loads(raw).get("vault") or {}) if status == 200 else {}
    if not check(status == 200 and vault.get("enabled") is True and not vault.get("error"),
                 "заметки Obsidian включены", vault.get("error") or vault.get("path", "")):
        return
    status, _ = http(port, "/api/memory/vault/sync", "POST", {}, headers={"Origin": "http://evil.example"})
    check(status == 403, "чужой сайт не трогает заметки Obsidian")
    status, raw = http(port, "/api/memory/vault/sync", "POST", {})
    check(status == 200 and json.loads(raw).get("ok") is True, "указатель Obsidian пересобирается",
          raw.decode("utf-8", "replace")[:200])
    root = Path(vault["path"])
    check((root / "INDEX.md").exists() and (root / "Джарвис").is_dir(), "указатель и зеркало memory.db в папке",
          str(root))

    reply, _ = ws_dialog(port, "запиши решение: " + SMOKE_DECISION[:1].lower() + SMOKE_DECISION[1:])
    check(reply is not None and "Записал решение" in reply.get("text", ""), "«запиши решение: …» без модели",
          (reply or {}).get("text", "нет ответа")[:80])
    notes = list((root / "Решения").glob("*.md"))
    check(any(SMOKE_DECISION in n.read_text(encoding="utf-8") for n in notes), "решение — файл Markdown в «Решения»",
          ", ".join(n.name for n in notes) or "файлов нет")
    status, raw = http(port, "/api/memory/brief")
    decisions = json.loads(raw).get("decisions", []) if status == 200 else []
    check(any(d.get("title") == SMOKE_DECISION for d in decisions), "панель памяти показывает решение",
          "; ".join(d.get("title", "") for d in decisions) or "решений нет")


ORB_INFO = "() => window.JarvisHUD && JarvisHUD.orb ? JarvisHUD.orb.info() : null"
# Доля голубых пикселей на снимке PNG: сфера голубая, «глаз» в теме «Ядро» красный. Счётчик кадров сферы
# считает проходы цикла, а не нарисованное, поэтому смотрим на сам снимок.
BLUE_SHARE = """async (b64) => {
  const img = new Image(); img.src = "data:image/png;base64," + b64; await img.decode();
  const c = document.createElement("canvas"); c.width = img.width; c.height = img.height;
  const ctx = c.getContext("2d"); ctx.drawImage(img, 0, 0);
  const d = ctx.getImageData(0, 0, c.width, c.height).data; let n = 0;
  for (let i = 0; i < d.length; i += 4) if (d[i + 2] > 110 && d[i + 2] > d[i] + 40) n++;
  return n / (d.length / 4);
}"""


def blue_share(page, selector: str) -> float:
    import base64
    return page.evaluate(BLUE_SHARE, base64.b64encode(page.locator(selector).screenshot()).decode())


def orb_and_memory(browser, port: int, out: Path) -> None:
    """Сфера рисует кадры (WebGL или запасной Canvas 2D) и следует за состоянием; панель памяти; без ошибок JS."""
    errors: list[str] = []
    try:
        for name, query in (("webgl", ""), ("canvas", "?orb=canvas")):
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("pageerror", lambda e: errors.append(str(e)[:200]))
            page.goto(f"http://127.0.0.1:{port}/{query}")
            page.wait_for_timeout(2500)
            info = page.evaluate(ORB_INFO) or {}
            # drawn обнуляется, когда сфера сама снижает качество на медленной машине
            drawing = info.get("drawn", 0) > 0 or bool(info.get("degraded"))
            want = ("canvas",) if name == "canvas" else ("webgl", "canvas")
            check(info.get("renderer") in want and drawing, f"сфера рисует кадры ({name})",
                  f"{info.get('renderer')}, качество {info.get('quality')}, {info.get('drawn')} кадров, "
                  f"{info.get('fps')} к/с, {info.get('gpu') or 'GPU неизвестен'}"
                  + (f", снижено: {'; '.join(info['degraded'])}" if info.get("degraded") else ""))
            page.evaluate("() => JarvisHUD.handle({type: 'state', state: 'speaking'})")
            page.wait_for_timeout(1200)
            info = page.evaluate(ORB_INFO) or {}
            check(info.get("state") == "speaking", f"сфера следует за состоянием ({name})", str(info.get("state")))
            page.locator("#core").screenshot(path=str(out / f"hud-orb-{name}.png"))
            shown = blue_share(page, "#core")
            # как пользователь: «Система → Сфера → Без сферы (простой глаз)»; вкладка скрыта, поэтому через change
            page.evaluate("() => { const s = document.getElementById('orbQuality'); s.value = 'off';"
                          " s.dispatchEvent(new Event('change')); }")
            page.wait_for_timeout(300)
            hidden = blue_share(page, "#core")
            check(shown > 0.003 and hidden < shown / 3, f"сфера видна на экране ({name})",
                  f"голубых пикселей со сферой {shown:.2%}, без сферы {hidden:.2%}")
            if name == "webgl":
                summary = page.locator("#mbDecSum").inner_text()
                check(SMOKE_DECISION in summary, "панель памяти в HUD: последнее решение", summary[:80])
                page.locator("#memBrief").screenshot(path=str(out / "hud-memory.png"))
            page.close()
    except Exception as e:  # noqa: BLE001 — браузер запустился, значит сбой здесь — ошибка HUD
        check(False, "сфера и панель памяти в браузере", str(e)[:200])
    check(not errors, "HUD без ошибок JavaScript", "; ".join(errors)[:400])


def screenshots(port: int) -> None:
    """Снимки HUD с настоящими шрифтами системы — в data/hud-*.png (артефакт CI)."""
    out = ROOT / "data"
    out.mkdir(exist_ok=True)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chromium")
            try:
                for name, size, action in (("wide", (1440, 900), None), ("blue", (1440, 900), "blue"),
                                           ("system", (1440, 900), "system"), ("laptop", (1280, 720), None),
                                           ("phone", (390, 844), None)):
                    page = browser.new_page(viewport={"width": size[0], "height": size[1]})
                    page.goto(f"http://127.0.0.1:{port}/")
                    page.wait_for_timeout(1500)
                    if action == "blue":
                        page.click("#themeBtn")
                    elif action == "system":
                        page.click('.tab[data-tab="diag"]')
                        page.wait_for_timeout(2500)
                    page.wait_for_timeout(300)
                    page.screenshot(path=str(out / f"hud-{name}.png"), full_page=name == "phone")
                    if action == "blue":
                        page.click("#themeBtn")  # тема хранится в браузере — вернуть как было
                    page.close()
                orb_and_memory(browser, port, out)
            finally:
                browser.close()
        warn(True, "снимки HUD", ", ".join(sorted(p.name for p in out.glob("hud-*.png"))))
    except Exception as e:  # noqa: BLE001 — без браузера не проверить ни сферу, ни поиск: это сбой установки
        check(False, "браузер для снимков и проверки сферы", str(e)[:200])


def wait_port_closed(port: int, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                time.sleep(0.5)
        except OSError:
            return True
    return False


def restart_after_update(port: int, proc: subprocess.Popen) -> None:
    """Кнопка «Обновить и перезапустить»: выход с кодом 75 → супервизор → снова HUD."""
    info = json.loads(http(port, "/api/update")[1])
    check(info.get("supervised") is True, "HUD знает, что запущен под супервизором")
    status, raw = http(port, "/api/update/apply", "POST", {}, headers={"Origin": "http://evil.example"})
    check(status == 403, "чужой сайт не может запустить обновление")
    status, raw = http(port, "/api/update/apply", "POST", {})
    if not check(status == 200, "обновление и перезапуск запускаются", raw.decode("utf-8", "replace")[:200]):
        return
    check(wait_port_closed(port, 90), "Джарвис завершился для обновления")
    if not check(wait_port(port, proc, 180), "супервизор запустил Джарвиса снова"):
        return
    info = json.loads(http(port, "/api/update")[1])
    result = info.get("result") or {}
    check(result.get("dry_run") is True and "Проверка без изменений" in result.get("message", ""),
          "HUD показывает итог обновления после перезапуска", result.get("message", "нет итога"))
    reply, _ = ws_dialog(port, "сколько будет 6 умножить на 7")
    check(reply is not None and "42" in reply.get("text", ""), "после перезапуска команды работают",
          (reply or {}).get("text", "нет ответа")[:80])


def stop_tree(proc: subprocess.Popen) -> None:
    """Остановить супервизор и запущенного им Джарвиса (иначе он займёт порт следующей проверки)."""
    try:
        import psutil
        children = psutil.Process(proc.pid).children(recursive=True)
    except ImportError:  # без psutil — вся группа процессов разом
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        else:
            import signal
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        children = []
    except Exception:  # noqa: BLE001 — процесс уже завершился
        children = []
    for child in children:
        try:
            child.terminate()
        except Exception:  # noqa: BLE001
            pass
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(20)
        except subprocess.TimeoutExpired:
            proc.kill()
    for child in children:
        try:
            child.wait(20)
        except Exception:  # noqa: BLE001
            pass


def finish(proc: subprocess.Popen, work: Path, log_out, keep: bool) -> int:
    stop_tree(proc)
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
