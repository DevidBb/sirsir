"""Локальная память и настройки в SQLite."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .security import looks_secret
from .tool_registry import Param, Tool, ToolRegistry

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reminders (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    text    TEXT NOT NULL,
    kind    TEXT NOT NULL DEFAULT 'reminder',
    due_at  REAL NOT NULL,
    done    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);
"""

CREATE_TASKS = """
CREATE TABLE IF NOT EXISTS task_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    request    TEXT NOT NULL,
    result     TEXT NOT NULL,
    tools      TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
"""

# Полнотекстовый индекс по фактам, предпочтениям, заметкам и итогам задач.
# Поддерживается триггерами SQLite — модули, которые пишут в эти таблицы, о нём не знают.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5(
    text, kind UNINDEXED, ref UNINDEXED, tokenize = 'unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO mem_fts(text, kind, ref) VALUES (new.text, new.kind, new.id); END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    DELETE FROM mem_fts WHERE kind IN ('fact', 'preference') AND ref = old.id; END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    DELETE FROM mem_fts WHERE kind IN ('fact', 'preference') AND ref = old.id;
    INSERT INTO mem_fts(text, kind, ref) VALUES (new.text, new.kind, new.id); END;
CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO mem_fts(text, kind, ref) VALUES (new.text, 'note', new.id); END;
CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
    DELETE FROM mem_fts WHERE kind = 'note' AND ref = old.id; END;
CREATE TRIGGER IF NOT EXISTS tasks_ai AFTER INSERT ON task_log BEGIN
    INSERT INTO mem_fts(text, kind, ref) VALUES (new.request || ' — ' || new.result, 'task', new.id); END;
CREATE TRIGGER IF NOT EXISTS tasks_ad AFTER DELETE ON task_log BEGIN
    DELETE FROM mem_fts WHERE kind = 'task' AND ref = old.id; END;
"""
FACT_KINDS = ("fact", "preference")
KIND_TITLES = {"preference": "предпочтение", "fact": "факт", "note": "заметка", "task": "задача"}
MAX_TASKS = 500
# До стольких фактов они все идут в промпт; больше — только найденные по запросу.
SMALL_MEMORY = 15
# Служебные слова не участвуют в поиске по памяти.
STOP_WORDS = set("""и в во на с со по о об от до за из к ко у для что как это мне меня мой моя мои
ты вы он она они мы не да нет ли же бы то так все всё или а но про при над под какой какая какие
the a an and or to of in on for is are what how my me""".split())

# Настройки, которые можно менять голосом: ключ → (тип, мин, макс, описание).
VOICE_SETTINGS: dict[str, tuple[type, float, float, str]] = {
    "tts.rate": (int, 80, 350, "скорость речи"),
    "tts.volume": (float, 0.1, 1.0, "громкость речи"),
    "llm.num_predict": (int, 50, 600, "максимальная длина ответа"),
    "assistant.follow_up_seconds": (int, 0, 30, "время ожидания продолжения диалога"),
}


class Memory:
    def __init__(self, db_path: str | Path, max_facts: int = 200):
        self.path = Path(db_path)
        if str(db_path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_facts = max_facts
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._migrate()
        self._db.commit()

    def _migrate(self) -> None:
        """Новые столбцы и таблицы для старых баз; полнотекстовый индекс (если есть FTS5)."""
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(facts)")}
        if "kind" not in cols:
            self._db.execute("ALTER TABLE facts ADD COLUMN kind TEXT NOT NULL DEFAULT 'fact'")
        self._db.executescript(CREATE_TASKS)
        try:
            self._db.executescript(FTS_SCHEMA)
            self.fts = True
        except sqlite3.OperationalError:  # SQLite без FTS5 — поиск через LIKE
            self.fts = False
            return
        if not self._db.execute("SELECT 1 FROM mem_fts LIMIT 1").fetchone():
            self._db.executescript("""
                INSERT INTO mem_fts(text, kind, ref) SELECT text, kind, id FROM facts;
                INSERT INTO mem_fts(text, kind, ref) SELECT text, 'note', id FROM notes;
                INSERT INTO mem_fts(text, kind, ref) SELECT request || ' — ' || result, 'task', id
                    FROM task_log;""")

    def close(self) -> None:
        # Под блокировкой: фоновые потоки (напоминания) не попадут в середину закрытия.
        with self._lock:
            self._db.close()

    def execute(self, sql: str, params: tuple = ()) -> list[tuple]:
        """Запрос под общей блокировкой (для модулей, хранящих данные в той же БД)."""
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
            self._db.commit()
            return rows

    def modify(self, sql: str, params: tuple = ()) -> int:
        """Изменяющий запрос; возвращает число затронутых строк."""
        with self._lock:
            count = self._db.execute(sql, params).rowcount
            self._db.commit()
            return count

    # --- настройки ---
    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (key, json.dumps(value, ensure_ascii=False)))
            self._db.commit()

    def all_settings(self) -> dict[str, Any]:
        with self._lock:
            rows = self._db.execute("SELECT key, value FROM settings").fetchall()
        return {k: json.loads(v) for k, v in rows}

    # --- факты ---
    def add_fact(self, text: str, kind: str = "fact") -> bool:
        text = " ".join(text.split())[:300]
        if not text or looks_secret(text):
            return False
        kind = kind if kind in FACT_KINDS else "fact"
        with self._lock:
            cur = self._db.execute("INSERT OR IGNORE INTO facts(text, created_at, kind) VALUES(?, ?, ?)",
                                   (text, time.time(), kind))
            # Ограничиваем размер памяти: удаляем самые старые факты.
            self._db.execute("DELETE FROM facts WHERE id NOT IN "
                             "(SELECT id FROM facts ORDER BY id DESC LIMIT ?)", (self.max_facts,))
            self._db.commit()
            return cur.rowcount > 0

    def facts(self, limit: int = 30) -> list[str]:
        with self._lock:
            rows = self._db.execute("SELECT text FROM facts ORDER BY id DESC LIMIT ?",
                                    (limit,)).fetchall()
        return [r[0] for r in reversed(rows)]

    def list_facts(self) -> list[dict[str, Any]]:
        rows = self.execute("SELECT id, text, created_at, kind FROM facts ORDER BY id DESC")
        return [{"id": r[0], "text": r[1], "created_at": r[2], "kind": r[3]} for r in rows]

    def update_fact(self, fact_id: int, text: str, kind: str | None = None) -> bool:
        text = " ".join(text.split())[:300]
        if not text or looks_secret(text):
            return False
        if kind in FACT_KINDS:
            return self.modify("UPDATE facts SET text = ?, kind = ? WHERE id = ?",
                               (text, kind, int(fact_id))) > 0
        return self.modify("UPDATE facts SET text = ? WHERE id = ?", (text, int(fact_id))) > 0

    def preferences(self, limit: int = 20) -> list[str]:
        rows = self.execute("SELECT text FROM facts WHERE kind = 'preference' ORDER BY id DESC LIMIT ?",
                            (limit,))
        return [r[0] for r in reversed(rows)]

    # --- итоги задач ---
    def add_task(self, request: str, result: str, tools: list[str] | None = None) -> int | None:
        """Краткий итог многошаговой задачи; секреты не сохраняются."""
        request = " ".join(request.split())[:300]
        result = " ".join(result.split())[:500]
        if not request or not result or looks_secret(request) or looks_secret(result):
            return None
        with self._lock:
            cur = self._db.execute("INSERT INTO task_log(request, result, tools, created_at) "
                                   "VALUES(?, ?, ?, ?)",
                                   (request, result, ",".join(tools or []), time.time()))
            self._db.execute("DELETE FROM task_log WHERE id NOT IN "
                             "(SELECT id FROM task_log ORDER BY id DESC LIMIT ?)", (MAX_TASKS,))
            self._db.commit()
            return cur.lastrowid

    def list_tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.execute("SELECT id, request, result, tools, created_at FROM task_log "
                            "ORDER BY id DESC LIMIT ?", (limit,))
        return [{"id": r[0], "request": r[1], "result": r[2], "tools": r[3], "created_at": r[4]}
                for r in rows]

    # --- поиск по памяти ---
    @staticmethod
    def _terms(query: str) -> list[str]:
        import re
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) >= 3 and w not in STOP_WORDS]
        # Грубая основа слова: «ответы», «ответов», «ответ» находят друг друга.
        def stem(w: str) -> str:
            if len(w) <= 3:
                return w
            return w[:-1] if len(w) <= 5 else w[:max(4, len(w) - 2)]
        return list(dict.fromkeys(stem(w) for w in words))[:12]

    def search(self, query: str, limit: int = 8, kinds: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        terms = self._terms(query)
        if not terms:
            return []
        if self.fts:
            match = " OR ".join('"' + t.replace('"', "") + '"*' for t in terms)
            rows = self.execute("SELECT kind, ref, text FROM mem_fts WHERE mem_fts MATCH ? "
                                "ORDER BY bm25(mem_fts) LIMIT ?", (match, limit * 3))
        else:
            like = " OR ".join("text LIKE ?" for _ in terms)
            rows = self.execute(
                f"SELECT kind, id, text FROM facts WHERE {like} UNION ALL "
                f"SELECT 'note', id, text FROM notes WHERE {like} LIMIT ?",
                tuple(f"%{t}%" for t in terms) * 2 + (limit * 3,))
        out = [{"kind": k, "id": int(ref), "text": text, "title": KIND_TITLES.get(k, k)}
               for k, ref, text in rows if kinds is None or k in kinds]
        return out[:limit]

    def context_for(self, query: str, limit: int = 6) -> str:
        """Блок для системного промпта: предпочтения всегда, остальное — только относящееся к запросу."""
        prefs = self.preferences()
        facts = self.execute("SELECT text FROM facts WHERE kind = 'fact' ORDER BY id DESC LIMIT ?",
                             (SMALL_MEMORY + 1,))
        found = [m for m in self.search(query, limit + len(prefs)) if m["kind"] != "preference"]
        if len(facts) <= SMALL_MEMORY:
            # Фактов мало — дешевле знать их все (имя пользователя нужно и в «привет»).
            found = [{"title": "факт", "text": r[0]} for r in reversed(facts)] + \
                [m for m in found if m["kind"] != "fact"][:limit]
        else:
            found = found[:limit]
        lines = []
        if prefs:
            lines.append("Предпочтения пользователя (соблюдай их):\n" + "\n".join(f"- {p}" for p in prefs))
        if found:
            lines.append("Из памяти (факты о пользователе, заметки, прошлые задачи):\n" + "\n".join(
                f"- [{m['title']}] {m['text']}" for m in found))
        return "\n".join(lines)

    def delete_fact(self, fact_id: int) -> bool:
        return self.modify("DELETE FROM facts WHERE id = ?", (int(fact_id),)) > 0

    def forget_fact(self, fragment: str) -> int:
        with self._lock:
            cur = self._db.execute("DELETE FROM facts WHERE text LIKE ?", (f"%{fragment}%",))
            self._db.commit()
            return cur.rowcount

    def clear_facts(self) -> int:
        with self._lock:
            cur = self._db.execute("DELETE FROM facts")
            self._db.commit()
            return cur.rowcount

    # --- инструменты для LLM ---
    def register_tools(self, registry: ToolRegistry, on_setting_changed=None) -> None:
        def remember(fact: str, kind: str = "fact") -> str:
            if looks_secret(fact):
                return "Пароли, токены и ключи я в память не записываю — храните их в менеджере паролей."
            if not self.add_fact(fact, kind):
                return "Это я уже помню."
            return "Запомнил ваше предпочтение." if kind == "preference" else "Запомнил."

        def search_memory(query: str) -> str:
            found = self.search(query, 8)
            if not found:
                return "В памяти ничего похожего нет."
            return "Нашёл в памяти:\n" + "\n".join(f"- [{m['title']}] {m['text']}" for m in found)

        def forget(fragment: str) -> str:
            n = self.forget_fact(fragment)
            return f"Забыл записей: {n}." if n else "Ничего похожего в памяти нет."

        def change_setting(key: str, value: str) -> str:
            kind, lo, hi, title = VOICE_SETTINGS[key]
            try:
                parsed = kind(str(value).replace(",", "."))
            except ValueError:
                return f"Значение {value} не подходит для параметра «{title}»."
            if not lo <= parsed <= hi:
                return f"Параметр «{title}» должен быть от {lo:g} до {hi:g}."
            self.set_setting(key, parsed)
            if on_setting_changed:
                on_setting_changed(key, parsed)
            return f"Параметр «{title}» теперь {parsed:g}."

        registry.register(Tool(
            "remember_fact", "Запомнить надолго по просьбе пользователя: факт о нём или его "
            "предпочтение (как ему удобнее, чтобы ты делал)", remember,
            {"fact": Param("string", "Короткий факт или предпочтение", max_length=300),
             "kind": Param("string", "fact — факт, preference — предпочтение", required=False,
                           enum=list(FACT_KINDS))}, category="memory"))
        registry.register(Tool(
            "search_memory", "Найти в долговременной памяти: факты, предпочтения, заметки, итоги "
            "прошлых задач", search_memory,
            {"query": Param("string", "Что искать", max_length=200)}, category="memory", read_only=True))
        registry.register(Tool(
            "forget_fact", "Забыть факты, содержащие фрагмент текста", forget,
            {"fragment": Param("string", "Фрагмент текста факта", max_length=100)}))
        registry.register(Tool(
            "change_setting",
            "Изменить настройку: " + "; ".join(f"{k} — {v[3]} ({v[1]:g}..{v[2]:g})"
                                              for k, v in VOICE_SETTINGS.items()),
            change_setting,
            {"key": Param("string", "Ключ настройки", enum=list(VOICE_SETTINGS)),
             "value": Param("string", "Новое значение (число)", max_length=20)},
            dangerous=True, confirm_template="изменить настройку {key} на {value}"))
