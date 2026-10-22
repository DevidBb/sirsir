"""Память Джарвиса в Obsidian: папка заметок Markdown рядом с memory.db.

Источник истины — у каждой записи ровно один владелец, поэтому две расходящиеся версии
одной записи не возникают:

* **memory.db (SQLite)** владеет фактами, предпочтениями, напоминаниями, настройками и итогами
  задач. В хранилище они попадают только зеркалом ``Джарвис/*.md`` (только для чтения): файл
  пересобирается из базы, правки в нём не читаются. Если файл зеркала изменили руками, правка
  не теряется — её копия ложится рядом (``…-правка-<дата>.md``) и видна в указателе «Нужно разобрать».
* **Файлы хранилища** владеют проектными заметками: проекты, цели, задачи и следующие шаги,
  решения, результаты проверок (и любые заметки, которые человек пишет сам). В SQLite — только
  индекс этих файлов для поиска (таблица ``vault_notes``); он пересобирается из файлов,
  при расхождении правы файлы.

Заметки — обычный Markdown, который читают Obsidian, GitHub и любой редактор: frontmatter
(``title``, ``type``, ``status``, ``date``, ``updated``, ``project``, ``topic``, ``source``, ``tags``),
относительные markdown-ссылки (без wikilinks), строка ``Related:`` в конце, указатель
``INDEX.md`` в корне и в каждой папке. Статус отличает факт (проверено) от предположения,
плана и сделанного. Секреты не записываются и не индексируются.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from .memory import STATUS_TITLES, VAULT_TYPE_TITLES, Memory
from .security import looks_secret
from .tool_registry import Param, Tool, ToolRegistry

log = logging.getLogger(__name__)

# Тип заметки → папка, заголовок указателя, приставка имени файла.
TYPES: dict[str, tuple[str, str, str]] = {
    "project": ("Проекты", "Проекты", "проект"),
    "goal": ("Цели", "Цели", "цель"),
    "task": ("Задачи", "Задачи и следующие шаги", "задача"),
    "decision": ("Решения", "Решения", "решение"),
    "check": ("Проверки", "Результаты проверок", "проверка"),
}
TYPE_ALIASES = {"проект": "project", "цель": "goal", "задача": "task", "шаг": "task", "решение": "decision",
                "проверка": "check", "next": "task", "step": "task"}
STATUSES = tuple(STATUS_TITLES)
STATUS_ALIASES = {v: k for k, v in STATUS_TITLES.items()} | {"проверено": "fact", "выполнено": "done",
                                                             "готово": "done", "сделан": "done"}
DEFAULT_STATUS = {"project": "plan", "goal": "plan", "task": "plan", "decision": "fact", "check": "fact"}
MIRROR_DIR = "Джарвис"
EDIT_TAG = "правка"
MIRROR_FACTS = "факты-и-предпочтения.md"
MIRROR_TASKS = "итоги-задач.md"
INDEX = "INDEX.md"
INDEX_MARK = "<!-- jarvis:index — указатель пересобирается автоматически; свои заметки пишите в отдельные файлы -->"
MIRROR_MARK = "<!-- jarvis:generated — файл пересобирается из memory.db; правки здесь не сохранятся -->"
SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules"}
MAX_FILES = 5000
MAX_BYTES = 256_000
MAX_BODY = 4000
# Версия разбора заметок: другая — индекс пересобирается целиком (после обновления Джарвиса).
INDEX_VERSION = 2


def slugify(text: str, limit: int = 60) -> str:
    """Имя файла из заголовка: буквы (в т.ч. русские), цифры и дефисы."""
    s = re.sub(r"[^\w\s-]", "", text.lower().replace("ё", "е"))
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s[:limit].strip("-") or "заметка"


def norm_text(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower().replace("ё", "е")))


# ---------- frontmatter ----------
def _yaml_value(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_yaml_value(v) for v in value) + "]"
    text = str(value)
    if re.fullmatch(r"\d{4}-\d\d-\d\d", text) or re.fullmatch(r"[\w][\w .-]*", text) and not re.fullmatch(
            r"(?i)true|false|yes|no|null|~|on|off|\d[\d.]*", text) and not text.endswith((" ", ".")):
        return text
    return json.dumps(text, ensure_ascii=False)  # строка JSON — допустимая строка YAML


def dump_frontmatter(meta: dict[str, Any]) -> str:
    lines = [f"{k}: {_yaml_value(v)}" for k, v in meta.items() if v not in (None, "", [])]
    return "---\n" + "\n".join(lines) + "\n---\n"


def _parse_scalar(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        return [_parse_scalar(x) for x in re.findall(r'"(?:[^"\\]|\\.)*"|\'[^\']*\'|[^,]+', inner)] if inner else []
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        try:
            return json.loads(raw)
        except ValueError:
            return raw[1:-1]
    if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
        return raw[1:-1].replace("''", "'")
    return raw


def split_frontmatter(text: str) -> tuple[dict[str, Any], str, bool]:
    """(поля, тело, frontmatter был). Простые ``ключ: значение`` и списки; остальное пропускается."""
    text = text.lstrip("﻿").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return {}, text, False
    end = text.find("\n---", 4)
    if end < 0 or text[end + 4:end + 5] not in ("", "\n"):
        return {}, text, False
    meta: dict[str, Any] = {}
    key, block = None, None
    for line in text[4:end].split("\n"):
        if block is not None and (line[:1] in (" ", "\t") or not line.strip()):  # многострочное «description: |»
            meta[key] = (meta[key] + ("\n" if block == "|" else " ") if meta[key] else "") + line.strip()
            continue
        block = None
        if re.match(r"^\s*-\s", line) and key is not None:  # блочный список
            if not isinstance(meta.get(key), list):
                meta[key] = []
            meta[key].append(_parse_scalar(line.strip()[2:]))
            continue
        m = re.match(r"^([A-Za-zА-Яа-яЁё_][\w-]*)\s*:\s*(.*)$", line)
        if m:
            key = m.group(1)
            raw = m.group(2).strip()
            if re.fullmatch(r"[|>][+-]?", raw):
                block, meta[key] = raw[0], ""
            else:
                meta[key] = _parse_scalar(raw) if raw else ""
    return meta, text[end + 5:].lstrip("\n"), True


def split_raw_frontmatter(text: str) -> tuple[str, str]:
    """(frontmatter как написан, с разделителями; тело). Без frontmatter — ("", текст)."""
    text = text.lstrip("﻿").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return "", text
    end = text.find("\n---", 4)
    if end < 0 or text[end + 4:end + 5] not in ("", "\n"):
        return "", text
    return text[:end + 4] + "\n", text[end + 5:].lstrip("\n")


def set_fields(raw: str, fields: dict[str, Any]) -> str:
    """Поменять во frontmatter только эти поля. Остальное — свойства, добавленные в Obsidian, их типы,
    многострочные значения, комментарии — остаётся как написано."""
    fields = {k: v for k, v in fields.items() if v not in (None, "", [])}
    if not raw:
        return dump_frontmatter(fields)
    inner = raw.rstrip("\n").split("\n")[1:-1]
    for key, value in fields.items():
        line = f"{key}: {_yaml_value(value)}"
        i = next((n for n, s in enumerate(inner) if re.match(rf"{re.escape(key)}\s*:", s)), None)
        if i is None:
            inner.append(line)
            continue
        j = i + 1
        while j < len(inner) and (inner[j][:1] in (" ", "\t") or inner[j].startswith("- ")):
            j += 1  # продолжение прежнего многострочного значения или списка
        inner[i:j] = [line]
    return "---\n" + "\n".join(inner) + "\n---\n"


def same_title(a: str, b: str) -> bool:
    """Одна запись: без регистра, «ё» и лишних пробелов, но со знаками («C++» и «C#» — разные)."""
    norm = lambda s: " ".join(s.casefold().replace("ё", "е").split()).rstrip(" .!")  # noqa: E731
    return norm(a) == norm(b)


def _first_sentence(body: str, limit: int = 200) -> str:
    for line in body.split("\n"):
        s = line.strip()
        if (not s or s.startswith(("#", ">", "|", "<!--", "Related:", "Up:", "```", "---"))
                or looks_secret(s)):
            continue
        s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s)
        s = re.sub(r"^[-*+]\s+|^\d+[.)]\s+|[*_`]+", "", s).strip()
        if not s:
            continue
        m = re.match(r"(.+?[.!?])(\s|$)", s)
        s = m.group(1) if m else s
        return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"
    return ""


def _write(path: Path, text: str) -> None:
    """Атомарная запись UTF-8 с LF (одинаково на Windows и в git)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").lstrip("﻿").replace("\r\n", "\n")


def _link(target: str, base: str = "") -> str:
    """Относительная markdown-ссылка из папки base на путь target (оба — от корня хранилища).
    Буквы (и русские) остаются как есть — так ссылку читает человек; пробелы и скобки кодируются."""
    rel = os.path.relpath(target, base or ".").replace(os.sep, "/")
    return "".join(c if c.isalnum() or c in "/-._~" else urllib.parse.quote(c) for c in rel)


class Vault:
    def __init__(self, root: str | Path, memory: Memory, clock=time.time):
        self.root = Path(root).expanduser()
        self.memory = memory
        self.clock = clock
        self._lock = threading.RLock()
        self._scanned = 0.0
        self.last_error: str | None = None
        self.on_saved: Any = None  # событие для HUD: заметка сохранена
        self._synced = False
        self._projects: dict[str, str] = {}
        self._timer: threading.Timer | None = None
        self._timer_lock = threading.Lock()
        self._closed = False

    # ---------- общее ----------
    def today(self) -> str:
        return _dt.date.fromtimestamp(self.clock()).isoformat()

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def obsidian_uri(self, rel_path: str = INDEX) -> str:
        """Ссылка obsidian://open — открывает заметку в Obsidian (если папка открыта как хранилище)."""
        return "obsidian://open?path=" + urllib.parse.quote(str((self.root / rel_path).resolve()), safe="")

    def status(self) -> dict[str, Any]:
        rows = self.memory.execute("SELECT type, COUNT(*) FROM vault_notes GROUP BY type")
        return {"enabled": True, "path": str(self.root), "uri": self.obsidian_uri(),
                "counts": {t: n for t, n in rows}, "error": self.last_error}

    # ---------- синхронизация ----------
    def sync(self) -> dict[str, int]:
        """Папки, индекс файлов, зеркало memory.db, указатели. Можно вызывать сколько угодно раз.
        Индекс обновляется по времени и размеру файлов; целиком — только когда изменился разбор (INDEX_VERSION)."""
        with self._lock:
            try:
                for folder, _, _ in TYPES.values():
                    (self.root / folder).mkdir(parents=True, exist_ok=True)
                rebuild = self.memory.get_setting("vault.index_version") != INDEX_VERSION
                stats = self.scan(force=rebuild)
                if rebuild:
                    self.memory.set_setting("vault.index_version", INDEX_VERSION)
                stats["mirror"] = self.write_mirror()
                stats["indexes"] = self.write_indexes()
                self.last_error = None
                self._synced = True
                return stats
            except Exception as e:  # noqa: BLE001 — папка недоступна, база закрыта: помощник работает дальше
                self.last_error = f"{self.root}: {e}"
                log.warning("Хранилище Obsidian: %s", self.last_error)
                return {"error": 1}

    def sync_later(self, delay: float = 1.5) -> None:
        """Синхронизация в фоне после задачи или правки памяти (несколько подряд — одна запись)."""
        with self._timer_lock:
            if self._closed:
                return
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(delay, self.sync)
            self._timer.daemon = True
            self._timer.start()

    def close(self) -> None:
        """До закрытия memory.db: отложенная синхронизация иначе сработает на закрытой базе."""
        with self._timer_lock:
            self._closed = True
            if self._timer is not None:
                self._timer.cancel()
        with self._lock:  # идущая синхронизация доделает запись
            pass

    def refresh(self) -> None:
        """Перед задачей: подхватить правки, сделанные в Obsidian (по времени и размеру файлов — дёшево,
        поэтому каждый раз). Первый раз — полная синхронизация: папки, зеркало, указатели."""
        if not self._synced:
            self.sync()
            return
        with self._lock:
            try:
                self.scan()
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)

    def _files(self) -> list[Path]:
        out: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            rel = Path(dirpath).relative_to(self.root).parts
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            if rel[:1] == (MIRROR_DIR,):  # зеркало memory.db — не заметки хранилища
                dirnames[:] = []
                continue
            for name in sorted(filenames):
                if name.lower().endswith(".md") and name != INDEX:
                    out.append(Path(dirpath) / name)
                    if len(out) >= MAX_FILES:
                        return out
        return out

    def scan(self, force: bool = False) -> dict[str, int]:
        """Индекс vault_notes ← файлы: новые и изменённые разобрать, удалённые убрать."""
        self._scanned = time.monotonic()
        seen, changed = set(), 0
        with self.memory.batch():  # одна транзакция на весь проход
            if force:
                self.memory.clear_vault_index()
            known = {n["path"]: (n["mtime"], n["size"]) for n in self.memory.vault_notes()}
            for path in self._files():
                rel = self.rel(path)
                try:
                    st = path.stat()
                except OSError:  # удалили или переименовали во время прохода
                    continue
                seen.add(rel)
                if known.get(rel) == (st.st_mtime, st.st_size) or st.st_size > MAX_BYTES:
                    continue
                try:
                    note = self._parse(path, rel, st)
                except OSError:
                    continue
                self.memory.upsert_vault_note(note)
                changed += 1
            removed = [p for p in known if p not in seen]
            for p in removed:
                self.memory.delete_vault_note(p)
        return {"notes": len(seen), "changed": changed, "removed": len(removed)}

    def _parse(self, path: Path, rel: str, st: os.stat_result) -> dict[str, Any]:
        meta, body, _ = split_frontmatter(_read(path))
        # Строки, похожие на пароли и ключи, в индекс (а значит, и в промпт модели и в INDEX.md) не попадают:
        # ни строки текста, ни заголовок, ни свойства.
        clean = "\n".join(line for line in body.split("\n") if not looks_secret(line))
        safe = lambda value: "" if looks_secret(str(value or "")) else str(value or "")  # noqa: E731
        h1 = re.search(r"^#\s+(.+)$", clean, re.M)
        title = (safe(meta.get("title")) or (h1.group(1).strip() if h1 else "") or safe(path.stem) or "заметка")[:200]
        kind = str(meta.get("type") or "").strip().lower()
        kind = TYPE_ALIASES.get(kind, kind)
        status = str(meta.get("status") or "").strip().lower()
        status = STATUS_ALIASES.get(status, status)
        return {"path": rel, "title": title, "type": kind if kind in VAULT_TYPE_TITLES else "note",
                "status": status if status in STATUSES else "",
                "project": safe(meta.get("project")), "topic": safe(meta.get("topic")),
                "date": str(meta.get("date") or ""), "updated": str(meta.get("updated") or meta.get("date") or ""),
                "summary": safe(meta.get("description")) or _first_sentence(clean),
                "body": clean[:MAX_BODY], "mtime": st.st_mtime, "size": st.st_size}

    # ---------- запись заметок ----------
    def note_path(self, kind: str, title: str, date: str) -> Path:
        folder, _, prefix = TYPES[kind]
        slug = slugify(title)
        name = f"{prefix}-{date}-{slug}.md" if kind == "decision" else f"{prefix}-{slug}.md"
        path = self.root / folder / name
        n = 2
        while kind == "decision" and path.exists():
            path = self.root / folder / f"{prefix}-{date}-{slug}-{n}.md"
            n += 1
        return path

    def save(self, kind: str, title: str, text: str = "", status: str = "", project: str = "",
             topic: str = "", source: str = "") -> dict[str, Any]:
        """Сохранить заметку. Возвращает {"ok", "text", "note"}; повтор не создаёт новую запись,
        новое решение по той же теме заменяет старое (старое получает status: superseded)."""
        kind = TYPE_ALIASES.get(kind.strip().lower(), kind.strip().lower())
        status = STATUS_ALIASES.get(status.strip().lower(), status.strip().lower()) if status else ""
        title = " ".join(title.split())[:120]
        text = text.strip()[:2000]
        source = " ".join(source.split())[:200]
        project = " ".join(project.split())[:120]
        if kind not in TYPES:
            return {"ok": False, "text": "Тип заметки: project, goal, task, decision или check."}
        if status and status not in STATUSES:
            return {"ok": False, "text": "Статус: fact, assumption, plan, done или superseded."}
        if not title:
            return {"ok": False, "text": "Нужен заголовок заметки."}
        if any(looks_secret(x) for x in (title, text, source, project, topic)):
            return {"ok": False, "text": "Похоже на пароль, ключ или номер карты — в заметки это не записываю."}
        status = status or DEFAULT_STATUS[kind]
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            self.scan()
            project_rel = self._ensure_project(project, source) if project and kind != "project" else ""
            if kind == "decision":
                result = self._save_decision(title, text, status, project_rel, topic, source)
            else:
                result = self._save_note(kind, title, text, status, project_rel, source)
            self.write_indexes()
        if result.get("ok") and self.on_saved:
            try:
                self.on_saved(result["note"])
            except Exception:  # noqa: BLE001 — HUD не должен мешать записи
                log.debug("Событие сохранения заметки", exc_info=True)
        return result

    def _project_slug(self, project_rel: str) -> str:
        return Path(project_rel).stem if project_rel else ""

    def _ensure_project(self, project: str, source: str) -> str:
        """Путь заметки проекта: найти по заголовку или имени файла, иначе создать."""
        stem = project.casefold()
        for n in self.memory.vault_notes("type = 'project'"):
            if same_title(n["title"], project) or Path(n["path"]).stem.casefold() in (stem, f"проект-{stem}"):
                return n["path"]
        path = self._own_path(self.note_path("project", project, self.today()), project)
        if not path.exists():
            self._create(path, {"title": project, "type": "project", "status": "plan"},
                         "Проект создан автоматически, когда к нему сохранили первую запись. "
                         "Опишите здесь цель и рамки проекта.", source, [])
        return self.rel(path)

    def _meta(self, title: str, kind: str, status: str, date: str, project_rel: str, topic: str,
              source: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        meta: dict[str, Any] = {"title": title, "type": kind, "status": status, "date": date, "updated": date}
        if project_rel:
            meta["project"] = self._project_slug(project_rel)
        if topic:
            meta["topic"] = topic
        if source:
            meta["source"] = source
        meta["tags"] = ["jarvis", VAULT_TYPE_TITLES[kind]]
        meta.update(extra or {})
        return meta

    def _related(self, rel: str, links: list[tuple[str, str]]) -> str:
        base = str(Path(rel).parent)
        folder = TYPES.get(self._type_of(rel), ("",))[0]
        parts = [f"[{t}]({_link(p, base)})" for t, p in links if p]
        if folder:
            parts.append(f"[{TYPES[self._type_of(rel)][1]}]({_link(folder + '/' + INDEX, base)})")
        return "Related: " + " · ".join(parts)

    @staticmethod
    def _type_of(rel: str) -> str:
        top = rel.split("/", 1)[0]
        return next((k for k, v in TYPES.items() if v[0] == top), "note")

    def _create(self, path: Path, meta: dict[str, Any], text: str, source: str,
                links: list[tuple[str, str]]) -> None:
        kind = meta["type"]
        date = self.today()
        full = self._meta(meta["title"], kind, meta["status"], date, meta.get("project_rel", ""),
                          meta.get("topic", ""), source,
                          {k: v for k, v in meta.items() if k not in ("title", "type", "status", "project_rel",
                                                                      "topic")})
        rel = self.rel(path)
        body = f"# {meta['title']}\n\n" + (text.strip() + "\n\n" if text.strip() else "") + self._related(rel, links) + "\n"
        _write(path, dump_frontmatter(full) + "\n" + body)
        self.memory.upsert_vault_note(self._parse(path, rel, path.stat()))

    def _title_of(self, rel: str) -> str:
        rows = self.memory.vault_notes("path = ?", (rel,))
        return rows[0]["title"] if rows else Path(rel).stem

    def _file_title(self, path: Path) -> str:
        meta, body, _ = split_frontmatter(_read(path))
        h1 = re.search(r"^#\s+(.+)$", body, re.M)
        return str(meta.get("title") or (h1.group(1).strip() if h1 else path.stem))

    def _own_path(self, path: Path, title: str) -> Path:
        """Файл этой записи: «C++» и «C#», «???» и «!!!» дают одно имя файла — другой записи суффикс -2, -3…"""
        base, n = path.stem, 2
        while path.exists() and not same_title(self._file_title(path), title):
            path = path.with_name(f"{base}-{n}.md")
            n += 1
        return path

    def _save_note(self, kind: str, title: str, text: str, status: str, project_rel: str,
                   source: str) -> dict[str, Any]:
        path = self._own_path(self.note_path(kind, title, self.today()), title)
        rel = self.rel(path)
        links = [(self._title_of(project_rel), project_rel)] if project_rel else []
        if not path.exists():
            self._create(path, {"title": title, "type": kind, "status": status, "project_rel": project_rel},
                         text, source, links)
            return {"ok": True, "text": f"Записал: {VAULT_TYPE_TITLES[kind]} «{title}» ({STATUS_TITLES[status]}).",
                    "note": self._note_info(rel), "created": True}
        meta, body, _ = split_frontmatter(_read(path))
        old_status = str(meta.get("status") or "")
        same_text = not text or norm_text(text) in norm_text(body)
        if same_text and old_status == status:
            return {"ok": True, "text": f"Это уже записано: «{title}».", "note": self._note_info(rel),
                    "created": False}
        today = self.today()
        head = f"## {today} — {STATUS_TITLES[status]}" + (f" (было: {STATUS_TITLES.get(old_status, old_status)})"
                                                         if old_status and old_status != status else "")
        section = head + (f"\n\nИсточник: {source}" if source else "") + ("\n\n" + text if not same_text else "") + "\n"
        fields: dict[str, Any] = {"status": status, "updated": today, "source": source}
        if project_rel and not meta.get("project"):
            fields["project"] = self._project_slug(project_rel)
        raw, body = split_raw_frontmatter(_read(path))
        body = self._insert_before_related(body, section)
        _write(path, set_fields(raw, fields) + "\n" + body)
        self.memory.upsert_vault_note(self._parse(path, rel, path.stat()))
        return {"ok": True, "text": f"Обновил: {VAULT_TYPE_TITLES[kind]} «{title}» ({STATUS_TITLES[status]}).",
                "note": self._note_info(rel), "created": False}

    @staticmethod
    def _insert_before_related(body: str, section: str) -> str:
        lines = body.rstrip("\n").split("\n")
        idx = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].startswith("Related:")), len(lines))
        before = "\n".join(lines[:idx]).rstrip("\n")
        after = "\n".join(lines[idx:])
        return before + "\n\n" + section.rstrip("\n") + "\n" + ("\n" + after + "\n" if after else "")

    def _save_decision(self, title: str, text: str, status: str, project_rel: str, topic: str,
                       source: str) -> dict[str, Any]:
        explicit = bool(topic.strip())
        topic = slugify(topic if explicit else title, limit=200)
        active = [n for n in self.memory.vault_notes("type = 'decision' AND topic = ? AND status != 'superseded'",
                                                     (topic,), order="date DESC, id DESC")]
        if not explicit:  # тема — сам заголовок: заменяет только решение с тем же заголовком, не с похожим slug
            active = [n for n in active if same_title(n["title"], title)]
        for n in active:
            if norm_text(n["title"]) == norm_text(title) and (not text or norm_text(text) in norm_text(n["body"])):
                if n["status"] != status and status in ("fact", "assumption"):
                    self._set_meta(n["path"], status=status, updated=self.today())
                    return {"ok": True, "text": f"Решение «{title}» теперь: {STATUS_TITLES[status]}.",
                            "note": self._note_info(n["path"]), "created": False}
                return {"ok": True, "text": f"Это решение уже записано: «{title}».",
                        "note": self._note_info(n["path"]), "created": False}
        path = self.note_path("decision", title, self.today())
        rel = self.rel(path)
        links = [(self._title_of(project_rel), project_rel)] if project_rel else []
        extra: dict[str, Any] = {}
        if active:
            extra["supersedes"] = [Path(n["path"]).stem for n in active]
            links += [(f"Заменяет: {n['title']}", n["path"]) for n in active]
        self._create(path, {"title": title, "type": "decision", "status": status, "project_rel": project_rel,
                            "topic": topic, **extra}, text, source, links)
        for n in active:  # одна действующая запись на тему: прежние помечаются заменёнными
            self._set_meta(n["path"], status="superseded", superseded_by=path.stem, updated=self.today(),
                           note=f"> [!NOTE]\n> Заменено решением [{title}]({_link(rel, str(Path(n['path']).parent))}) "
                                f"от {self.today()}.")
        replaced = f" Прежнее решение по этой теме помечено как заменённое ({len(active)})." if active else ""
        return {"ok": True, "text": f"Записал решение «{title}» ({STATUS_TITLES[status]}).{replaced}",
                "note": self._note_info(rel), "created": True, "superseded": [n["path"] for n in active]}

    def _set_meta(self, rel: str, note: str = "", **fields: Any) -> None:
        path = self.root / rel
        raw, body = split_raw_frontmatter(_read(path))
        if note and note not in body:
            h1 = re.search(r"^#\s+.+$", body, re.M)
            pos = h1.end() if h1 else 0
            body = body[:pos] + "\n\n" + note + "\n\n" + body[pos:].lstrip("\n")
        _write(path, set_fields(raw, fields) + "\n" + body.lstrip("\n"))
        self.memory.upsert_vault_note(self._parse(path, rel, path.stat()))

    def _note_info(self, rel: str) -> dict[str, Any]:
        rows = self.memory.vault_notes("path = ?", (rel,))
        n = rows[0] if rows else {"path": rel, "title": Path(rel).stem, "type": "note", "status": "", "date": "",
                                  "project": "", "summary": ""}
        return {"path": n["path"], "title": n["title"], "type": n["type"], "status": n["status"],
                "date": n.get("updated") or n["date"], "project": n["project"], "summary": n["summary"],
                "uri": self.obsidian_uri(n["path"])}

    # ---------- для HUD и промпта ----------
    def recent(self, kind: str, limit: int = 5, active_only: bool = True) -> list[dict[str, Any]]:
        where = "type = ?" + (" AND status != 'superseded'" if active_only else "")
        return [self._note_info(n["path"]) for n in
                self.memory.vault_notes(where, (kind,), order="updated DESC, date DESC, id DESC", limit=limit)]

    def next_steps(self, limit: int = 5) -> list[dict[str, Any]]:
        return [self._note_info(n["path"]) for n in
                self.memory.vault_notes("type = 'task' AND status = 'plan'", order="updated DESC, id DESC",
                                        limit=limit)]

    def related(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        out = []
        for m in self.memory.search(query, limit * 2, kinds=("vault",)):
            info = self._note_info(m["path"])
            if info not in out:
                out.append(info)
        return out[:limit]

    # ---------- зеркало memory.db (только чтение) ----------
    def write_mirror(self) -> int:
        """Факты, предпочтения и итоги задач из memory.db → Джарвис/*.md. Возвращает число записанных файлов."""
        facts = self.memory.list_facts()
        prefs = [f for f in facts if f["kind"] == "preference"]
        plain = [f for f in facts if f["kind"] == "fact"]

        def day(ts: float) -> str:
            return _dt.date.fromtimestamp(ts).isoformat()

        def items(rows: list[dict], fmt) -> str:
            return "\n".join(fmt(r) for r in rows) if rows else "- пока нет"

        note = ("> [!NOTE]\n> Источник истины — memory.db. Изменить или удалить: HUD → «Память» или голосом "
                "(«забудь …»). Правки в этом файле перезапишутся; если вы всё же правили его, копия правки "
                "сохранится рядом (…-правка-дата.md) и появится в указателе в разделе «Нужно разобрать».")
        facts_md = (dump_frontmatter({"title": "Факты и предпочтения", "type": "note", "status": "fact",
                                      "source": "memory.db", "tags": ["jarvis", "зеркало"]})
                    + f"\n# Факты и предпочтения\n\n{MIRROR_MARK}\n\n{note}\n\n## Предпочтения\n\n"
                    + items(prefs, lambda f: f"- {f['text']} ({day(f['created_at'])})")
                    + "\n\n## Факты\n\n" + items(plain, lambda f: f"- {f['text']} ({day(f['created_at'])})")
                    + f"\n\nRelated: [Память Джарвиса](../{INDEX}) · [Итоги задач]({_link(MIRROR_TASKS)})\n")
        tasks = self.memory.list_tasks(100)
        tasks_md = (dump_frontmatter({"title": "Итоги задач", "type": "note", "status": "done",
                                      "source": "memory.db", "tags": ["jarvis", "зеркало"]})
                    + f"\n# Итоги задач\n\n{MIRROR_MARK}\n\n{note}\n\nПоследние {len(tasks)} многошаговых задач: "
                      "запрос → результат (инструменты).\n\n"
                    + items(tasks, lambda t: f"- {day(t['created_at'])} · {t['request']} → {t['result']}"
                            + (f" ({t['tools']})" if t["tools"] else ""))
                    + f"\n\nRelated: [Память Джарвиса](../{INDEX}) · [Факты и предпочтения]({_link(MIRROR_FACTS)})\n")
        written = 0
        for name, text in ((MIRROR_FACTS, facts_md), (MIRROR_TASKS, tasks_md)):
            written += self._write_generated(self.root / MIRROR_DIR / name, text)
        return written

    def _write_generated(self, path: Path, text: str) -> int:
        """Записать файл зеркала; ручную правку сначала сохранить копией в правки/."""
        key = "vault.mirror." + path.name
        if path.exists():
            current = _read(path)
            if current == text:
                return 0
            last = self.memory.get_setting(key)
            if last != hashlib.sha256(current.encode("utf-8")).hexdigest():
                stamp = _dt.datetime.fromtimestamp(self.clock()).strftime("%Y-%m-%d-%H%M%S")
                copy = path.parent / f"{path.stem}-{EDIT_TAG}-{stamp}.md"  # рядом: ссылки внутри не ломаются
                _write(copy, current)
                log.warning("Файл зеркала %s изменён вручную — копия правки: %s", path.name, copy)
        _write(path, text)
        self.memory.set_setting(key, hashlib.sha256(text.encode("utf-8")).hexdigest())
        return 1

    def edits(self) -> list[str]:
        folder = self.root / MIRROR_DIR
        return sorted(self.rel(p) for p in folder.glob(f"*-{EDIT_TAG}-*.md")) if folder.is_dir() else []

    # ---------- указатели ----------
    def _line(self, n: dict[str, Any], base: str) -> str:
        bits = [STATUS_TITLES.get(n["status"], ""), n.get("updated") or n["date"]]
        if n["project"] and n["type"] != "project":
            bits.append("проект «" + self._projects.get(n["project"], n["project"].removeprefix("проект-")) + "»")
        meta = " · ".join(b for b in bits if b)
        summary = f" — {n['summary']}" if n["summary"] else ""
        return f"- [{n['title']}]({_link(n['path'], base)}): {meta}{summary}"

    def write_indexes(self) -> int:
        notes = self.memory.vault_notes(order="path")
        self._projects = {Path(n["path"]).stem: n["title"] for n in notes if n["type"] == "project"}
        written = 0
        for kind, (folder, title, _) in TYPES.items():
            rows = [n for n in notes if n["path"].startswith(folder + "/") and "/" not in n["path"][len(folder) + 1:]]
            if not rows and not (self.root / folder).is_dir():
                continue
            if kind == "decision":  # новые сверху, заменённые — в конце
                rows.sort(key=lambda n: (n["date"], n["path"]), reverse=True)
                rows.sort(key=lambda n: n["status"] == "superseded")
            else:
                order = {"plan": 0, "assumption": 1, "fact": 2, "done": 3, "superseded": 4}
                rows.sort(key=lambda n: (order.get(n["status"], 5), n["title"].lower()))
            body = "\n".join(self._line(n, folder) for n in rows) or "- пока пусто"
            text = (f"# {title}\n\n{INDEX_MARK}\n\n{body}\n\nRelated: [Память Джарвиса](../{INDEX})\n")
            written += self._write_index(self.root / folder / INDEX, text)
        written += self._write_index(self.root / INDEX, self._root_index(notes))
        return written

    def _write_index(self, path: Path, text: str) -> int:
        if path.exists():
            current = _read(path)
            if current == text:
                return 0
            if INDEX_MARK not in current:  # свой указатель пользователя не перезаписываем
                log.info("Указатель %s написан вручную — не перезаписываю", path)
                return 0
        _write(path, text)
        return 1

    def _root_index(self, notes: list[dict[str, Any]]) -> str:
        def pick(kind: str, status: str | None = None, active: bool = True) -> list[dict]:
            rows = [n for n in notes if n["type"] == kind and n["path"].startswith(TYPES[kind][0] + "/")
                    and (status is None or n["status"] == status) and (not active or n["status"] != "superseded")]
            return sorted(rows, key=lambda n: (n.get("updated") or n["date"], n["path"]), reverse=True)

        out = [f"# Память Джарвиса\n\n{INDEX_MARK}\n",
               "Вход в память для продолжения работы: проекты и цели, следующие шаги, решения и результаты "
               "проверок. Перед задачей Джарвис ищет здесь нужный контекст, после — сохраняет существенные итоги.\n",
               "Статусы: **факт** — проверено, есть источник; **предположение** — не проверено; **план** — ещё "
               "не сделано; **сделано** — выполнено; **заменено** — устарело, действует более новое решение.\n"]
        projects = pick("project", active=False)
        steps = pick("task", "plan")
        if projects:
            out.append("## Проекты\n")
            for p in sorted(projects, key=lambda n: (n["status"] == "done", n["title"].lower())):
                out.append(self._line(p, ""))
                slug = Path(p["path"]).stem
                for s in [s for s in steps if s["project"] == slug][:5]:
                    out.append("  " + self._line(s, "").replace("- [", "- следующий шаг: [", 1))
            out.append("")
        loose = [s for s in steps if not s["project"] or not any(Path(p["path"]).stem == s["project"]
                                                                  for p in projects)]
        if loose:
            out.append("## Следующие шаги\n")
            out += [self._line(s, "") for s in loose[:10]] + [""]
        for kind, head in (("goal", "Цели"), ("decision", "Последние решения"), ("check", "Последние проверки")):
            rows = pick(kind)[:10]
            if rows:
                out.append(f"## {head}\n")
                out += [self._line(n, "") for n in rows] + [""]
        out.append("## Разделы\n")
        for kind, (folder, title, _) in TYPES.items():
            if (self.root / folder).is_dir():
                count = sum(1 for n in notes if n["path"].startswith(folder + "/"))
                out.append(f"- [{title}]({_link(folder + '/' + INDEX)}): все записи этого типа ({count})")
        out.append(f"- [Факты и предпочтения]({_link(MIRROR_DIR + '/' + MIRROR_FACTS)}): из memory.db, только чтение")
        out.append(f"- [Итоги задач]({_link(MIRROR_DIR + '/' + MIRROR_TASKS)}): из memory.db, только чтение")
        edits = self.edits()
        if edits:
            out.append("\n## Нужно разобрать\n")
            out.append("Правки, сделанные вручную в файлах зеркала. Перенесите нужное в память через HUD → «Память» "
                       "и удалите копию.\n")
            out += [f"- [{Path(e).stem}]({_link(e)}): копия правки" for e in edits]
        out.append("\nПравила: у каждой записи один владелец. Факты, предпочтения и итоги задач живут в memory.db "
                   "(здесь — зеркало), проекты, цели, задачи, решения и проверки — в файлах этой папки "
                   "(memory.db хранит только индекс для поиска). Пароли, токены и ключи сюда не записываются.\n")
        return "\n".join(out)

    # ---------- инструмент для модели ----------
    def register_tools(self, registry: ToolRegistry) -> None:
        def save_memory_note(type: str, title: str, text: str = "", status: str = "", project: str = "",
                             topic: str = "", source: str = "") -> str:
            return self.save(type, title, text, status, project, topic, source or "разговор с Джарвисом")["text"]

        registry.register(Tool(
            "save_memory_note",
            "Сохранить в долговременную память (заметки Obsidian) существенный итог работы: принятое решение, "
            "результат проверки, цель, проект или следующий шаг. Только когда пользователь что-то решил, "
            "проверка дала результат или договорились о следующем шаге — не для обычных ответов. "
            "Повтор не создаёт новую запись; новое решение по той же теме заменяет прежнее.",
            save_memory_note,
            {"type": Param("string", "decision — решение, check — результат проверки, task — задача или "
                                     "следующий шаг, goal — цель, project — проект", enum=list(TYPES)),
             "title": Param("string", "Короткий заголовок", max_length=120),
             "text": Param("string", "Суть: что решено или проверено, почему, чем подтверждено", required=False,
                           max_length=2000),
             "status": Param("string", "fact — проверено; assumption — не проверено; plan — план, ещё не "
                                       "сделано; done — сделано", required=False,
                             enum=["fact", "assumption", "plan", "done"]),
             "project": Param("string", "Название проекта, к которому относится запись", required=False,
                              max_length=120),
             "topic": Param("string", "Тема решения: новое решение по той же теме заменит старое", required=False,
                            max_length=80),
             "source": Param("string", "Откуда известно: проверка, ссылка, слова пользователя", required=False,
                             max_length=200)},
            category="memory"))


def setup(config: Any, memory: Memory) -> Vault | None:
    """Хранилище из [memory.vault]; пустой путь — папка obsidian рядом с memory.db."""
    if not config.get("memory.vault.enabled", True):
        return None
    raw = str(config.get("memory.vault.path", "") or "").strip()
    db = Path(str(config.path("memory.db_path")))
    root = Path(raw).expanduser() if raw else db.parent / "obsidian"
    if raw and not root.is_absolute():
        root = Path(str(config.root)) / root
    return Vault(root, memory)
