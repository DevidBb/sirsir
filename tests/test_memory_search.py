"""Память: типы записей, полнотекстовый поиск, итоги задач, контекст в промпт."""

import sqlite3

import pytest

from conftest import FakeLLM, text, tool_call
from jarvis.assistant import Assistant
from jarvis.memory import SMALL_MEMORY, Memory
from jarvis.notes import Notes
from jarvis.tool_registry import Param, Tool, ToolRegistry


@pytest.fixture
def mem():
    return Memory(":memory:")


def test_search_finds_word_forms_and_kinds(mem):
    mem.add_fact("Предпочитаю короткие ответы", "preference")
    mem.add_fact("Меня зовут Давид")
    Notes(mem).add("купить молоко и хлеб")
    mem.add_task("найди видео про локальных ассистентов", "Нашёл 5 видео", ["youtube_search"])
    assert [m["kind"] for m in mem.search("какой ответ мне нужен")] == ["preference"]
    assert mem.search("как меня зовут")[0]["text"] == "Меня зовут Давид"
    assert mem.search("молоко")[0]["kind"] == "note"
    assert mem.search("ассистенты")[0]["kind"] == "task"
    assert mem.search("и в на") == []  # только служебные слова


def test_index_follows_deletes_and_updates(mem):
    notes = Notes(mem)
    notes.add("позвонить маме")
    notes.delete_last()
    assert mem.search("позвонить") == []
    mem.add_fact("Люблю чай")
    fid = mem.list_facts()[0]["id"]
    mem.update_fact(fid, "Люблю кофе", "preference")
    assert mem.search("чай") == [] and mem.search("кофе")[0]["kind"] == "preference"
    mem.delete_fact(fid)
    assert mem.search("кофе") == []


def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript("""CREATE TABLE facts (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL UNIQUE,
                        created_at REAL NOT NULL);
                        CREATE TABLE notes (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL,
                        created_at REAL NOT NULL);
                        INSERT INTO facts(text, created_at) VALUES ('Живу в Казани', 0);
                        INSERT INTO notes(text, created_at) VALUES ('пароль от wifi на холодильнике', 0);""")
    db.commit()
    db.close()
    mem = Memory(path)
    assert mem.list_facts()[0]["kind"] == "fact"
    assert mem.search("Казани")[0]["text"] == "Живу в Казани"  # старые данные проиндексированы
    assert mem.search("холодильнике")[0]["kind"] == "note"
    mem.close()


def test_task_summaries_skip_secrets_and_are_capped(mem):
    assert mem.add_task("войди с паролем hunter22", "готово") is None
    assert mem.add_task("", "x") is None
    tid = mem.add_task("сравни два источника про MCP", "MCP — протокол", ["internet_search", "read_page"])
    assert mem.list_tasks()[0] == {"id": tid, "request": "сравни два источника про MCP",
                                   "result": "MCP — протокол", "tools": "internet_search,read_page",
                                   "created_at": mem.list_tasks()[0]["created_at"]}


def test_context_small_memory_includes_all_facts(mem):
    mem.add_fact("Меня зовут Давид")
    mem.add_fact("Отвечай коротко", "preference")
    ctx = mem.context_for("привет")
    assert "Отвечай коротко" in ctx and "Меня зовут Давид" in ctx


def test_context_large_memory_only_relevant(mem):
    for i in range(SMALL_MEMORY + 5):
        mem.add_fact(f"Факт номер {i} про погоду")
    mem.add_fact("Мой кот Барсик любит рыбу")
    ctx = mem.context_for("как зовут кота")
    assert "Барсик" in ctx and ctx.count("погоду") <= 6


@pytest.fixture
def assistant(config):
    registry = ToolRegistry()
    m = Memory(":memory:")
    m.register_tools(registry)
    registry.register(Tool("lookup", "Найти", lambda q: f"нашёл {q}", {"q": Param("string", "q")}))
    return Assistant(config, FakeLLM([]), registry, m)


def test_remember_without_llm(assistant):
    reply = assistant.handle("Запомни, что я предпочитаю короткие ответы")
    assert reply.text == "Запомнил ваше предпочтение." and assistant.llm.requests == []
    assert assistant.memory.list_facts()[0]["kind"] == "preference"
    reply = assistant.handle("запомни меня зовут Давид")
    assert reply.text == "Запомнил." and assistant.memory.list_facts()[0]["kind"] == "fact"
    assert "не записываю" in assistant.handle("запомни мой пароль от почты qwerty123").text


def test_prompt_contains_preferences_and_task_is_saved(assistant):
    assistant.memory.add_fact("Отвечай коротко", "preference")
    assistant.llm.responses = [tool_call("lookup", q="MCP"), text("MCP — протокол.")]
    assistant.handle("найди и объясни подробно, что такое MCP")
    system = assistant.llm.requests[0]["messages"][0]["content"]
    assert "Отвечай коротко" in system
    task = assistant.memory.list_tasks()[0]
    assert task["result"] == "MCP — протокол." and task["tools"] == "lookup"


def test_plain_chat_is_not_saved_as_task(assistant):
    assistant.llm.responses = [text("Привет!")]
    assistant.handle("расскажи что-нибудь интересное про космос")
    assert assistant.memory.list_tasks() == []


def test_task_summaries_can_be_disabled(assistant, config):
    config.set("memory.task_summaries", False)
    assistant.llm.responses = [tool_call("lookup", q="x"), text("готово")]
    assistant.handle("найди что-то и подробно объясни")
    assert assistant.memory.list_tasks() == []


def test_search_memory_tool(assistant):
    assistant.memory.add_fact("Мой кот Барсик")
    out = assistant.registry.get("search_memory").handler(query="кот")
    assert "Барсик" in out and "[факт]" in out
    assert assistant.registry.get("search_memory").handler(query="самолёт") == "В памяти ничего похожего нет."
