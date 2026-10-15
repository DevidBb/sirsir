"""Голосовые заметки в SQLite («запиши заметку купить молоко»)."""

from __future__ import annotations

import datetime as _dt
import time

from .memory import Memory
from .tool_registry import Param, Tool, ToolRegistry

MAX_NOTES = 500


class Notes:
    def __init__(self, memory: Memory, clock=time.time):
        self.memory = memory
        self.clock = clock

    def add(self, text: str) -> str:
        text = " ".join(text.split())[:500]
        if not text:
            return "Пустую заметку записывать не буду."
        self.memory.modify("INSERT INTO notes(text, created_at) VALUES(?, ?)", (text, self.clock()))
        self.memory.modify("DELETE FROM notes WHERE id NOT IN "
                           "(SELECT id FROM notes ORDER BY id DESC LIMIT ?)", (MAX_NOTES,))
        return "Записал."

    def read(self, limit: int = 5) -> str:
        rows = self.memory.execute("SELECT text, created_at FROM notes ORDER BY id DESC LIMIT ?",
                                   (max(1, min(int(limit), 20)),))
        if not rows:
            return "Заметок нет."
        today = _dt.date.fromtimestamp(self.clock())
        items = []
        for text, created in rows:
            day = _dt.date.fromtimestamp(created)
            when = "сегодня" if day == today else ("вчера" if (today - day).days == 1
                                                   else f"{day.day}.{day.month:02d}")
            items.append(f"{when}: {text}")
        return "Последние заметки. " + ". ".join(items) + "."

    def search(self, query: str) -> str:
        rows = self.memory.execute("SELECT text FROM notes WHERE text LIKE ? ORDER BY id DESC LIMIT 5",
                                   (f"%{query}%",))
        return ("Нашёл: " + "; ".join(r[0] for r in rows) + ".") if rows else "Ничего не нашёл."

    def delete_last(self) -> str:
        n = self.memory.modify("DELETE FROM notes WHERE id = (SELECT MAX(id) FROM notes)")
        return "Последняя заметка удалена." if n else "Заметок нет."

    def clear(self) -> str:
        n = self.memory.modify("DELETE FROM notes")
        return f"Удалено заметок: {n}."

    def register_tools(self, registry: ToolRegistry) -> None:
        registry.register(Tool("add_note", "Записать заметку", self.add,
                               {"text": Param("string", "Текст заметки", max_length=500)}))
        registry.register(Tool("read_notes", "Прочитать последние заметки",
                               lambda limit=5: self.read(limit),
                               {"limit": Param("integer", "Сколько (1-20)", required=False)}))
        registry.register(Tool("search_notes", "Найти заметки по слову", self.search,
                               {"query": Param("string", "Слово для поиска", max_length=100)}))
        registry.register(Tool("delete_last_note", "Удалить последнюю заметку", self.delete_last,
                               dangerous=True, confirm_template="удалить последнюю заметку"))
        registry.register(Tool("clear_notes", "Удалить все заметки", self.clear,
                               dangerous=True, confirm_template="удалить все заметки"))
