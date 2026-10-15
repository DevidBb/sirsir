"""Локальная память и настройки в SQLite."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

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
        self._db.commit()

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
    def add_fact(self, text: str) -> bool:
        text = " ".join(text.split())[:300]
        if not text:
            return False
        with self._lock:
            cur = self._db.execute("INSERT OR IGNORE INTO facts(text, created_at) VALUES(?, ?)",
                                   (text, time.time()))
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
        def remember(fact: str) -> str:
            return "Запомнил." if self.add_fact(fact) else "Это я уже помню."

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
            "remember_fact", "Запомнить факт о пользователе надолго (по его просьбе)", remember,
            {"fact": Param("string", "Короткий факт", max_length=300)}))
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
