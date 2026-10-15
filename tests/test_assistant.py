from pathlib import Path

import pytest

from conftest import FakeLLM, text, tool_call
from jarvis.assistant import EXIT, MUTE, STOP, Assistant
from jarvis.llm import LLMError
from jarvis.memory import Memory
from jarvis.tool_registry import ToolRegistry
from jarvis.windows_control import WindowsController


def build(config, responses):
    launched, trashed = [], []
    llm = FakeLLM(responses)
    registry = ToolRegistry()
    WindowsController(config, launcher=launched.append, url_opener=lambda u: None,
                      trash=trashed.append).register_tools(registry)
    memory = Memory(config.path("memory.db_path"))
    memory.register_tools(registry)
    a = Assistant(config, llm, registry, memory)
    a.launched, a.trashed, a.fake = launched, trashed, llm
    return a


def test_plain_answer_and_history(config):
    a = build(config, [text("Привет!"), text("Всё хорошо.")])
    assert a.handle("привет").text == "Привет!"
    a.handle("как дела")
    msgs = a.fake.requests[1]["messages"]
    assert [m["content"] for m in msgs[1:]] == ["привет", "Привет!", "как дела"]


def test_safe_tool_runs_and_result_goes_back(config):
    a = build(config, [tool_call("open_app", name="блокнот"), text("Открыл блокнот.")])
    reply = a.handle("слушай, будь добр, запусти мне, пожалуйста, текстовый редактор блокнот")
    assert reply.text == "Открыл блокнот."
    assert a.launched == ["notepad.exe"]
    tool_msg = a.fake.requests[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and "Запускаю" in tool_msg["content"]


def test_dangerous_tool_requires_confirmation(config):
    f = Path(config.get("windows.search.allowed_dirs")[0]) / "old.txt"
    f.write_text("x")
    a = build(config, [tool_call("delete_file", path=str(f))])
    reply = a.handle("удали old.txt")
    assert "подтверждение" in reply.text.lower()
    assert a.trashed == []
    reply = a.handle("подтверждаю")
    assert "корзину" in reply.text
    assert len(a.trashed) == 1


def test_dangerous_tool_cancelled(config):
    f = Path(config.get("windows.search.allowed_dirs")[0]) / "old.txt"
    f.write_text("x")
    a = build(config, [tool_call("delete_file", path=str(f))])
    a.handle("удали old.txt")
    assert a.handle("отмена").text == "Действие отменено."
    assert a.trashed == []


def test_invalid_tool_call_reported_to_model(config):
    a = build(config, [tool_call("run_powershell", code="rm -rf /"), text("Не могу.")])
    assert a.handle("выполни скрипт").text == "Не могу."
    tool_msg = a.fake.requests[1]["messages"][-1]
    assert tool_msg["content"].startswith("Ошибка: Неизвестное действие")


def test_tool_loop_is_stopped(config):
    same = tool_call("current_time")
    a = build(config, [same, same, same, same])
    assert "зациклился" in a.handle("время").text


def test_too_many_tool_rounds(config):
    calls = [tool_call("search_files", query=f"q{i}") for i in range(10)]
    a = build(config, calls)
    assert "Слишком много действий" in a.handle("ищи").text
    assert len(a.fake.requests) == 4  # 1 + max_tool_calls_per_turn


def test_emergency_stop(config):
    a = build(config, [tool_call("delete_file", path="x")])
    a.handle("удали x")
    reply = a.handle("стоп стоп")
    assert reply.action == STOP
    assert not a.confirmations.has_pending()


def test_duplicate_command_ignored(config):
    a = build(config, [tool_call("open_app", name="калькулятор"), text("ок"), text("ок")])
    a.handle("открой калькулятор")
    assert a.handle("открой калькулятор") is None
    assert a.launched == ["calc.exe"]


@pytest.mark.parametrize("phrase,action", [("выключи микрофон", MUTE), ("заверши работу", EXIT)])
def test_builtin_commands_skip_llm(config, phrase, action):
    a = build(config, [])
    assert a.handle(phrase).action == action
    assert a.fake.requests == []


def test_clear_memory_needs_confirmation(config):
    a = build(config, [])
    a.memory.add_fact("люблю чай")
    a.handle("очисти память")
    assert a.memory.facts() == ["люблю чай"]
    assert "удалено записей: 1" in a.handle("подтверждаю").text
    assert a.memory.facts() == []


def test_facts_in_system_prompt(config):
    a = build(config, [text("ок")])
    a.memory.add_fact("пользователя зовут Давид")
    a.handle("как меня зовут")
    assert "Давид" in a.fake.requests[0]["messages"][0]["content"]


def test_llm_unavailable(config):
    a = build(config, [LLMError("down")])
    assert "Ollama" in a.handle("привет").text
    assert len(a.conversation) == 0


def test_cancel_phrase(config):
    a = build(config, [])
    assert "отменяю" in a.handle("отмена").text
    assert a.fake.requests == []


def test_quick_intent_skips_llm(config):
    a = build(config, [])
    reply = a.handle("Открой блокнот")
    assert reply.text == "Запускаю блокнот."
    assert a.launched == ["notepad.exe"]
    assert a.fake.requests == []
    # результат попадает в историю для следующих реплик
    assert len(a.conversation) == 1


def test_unknown_app_goes_to_llm(config):
    a = build(config, [text("Такого приложения нет в списке.")])
    assert a.handle("открой фотошоп").text == "Такого приложения нет в списке."
    assert a.launched == []


def test_actions_are_audited(config, caplog):
    a = build(config, [])
    with caplog.at_level("INFO", logger="jarvis.audit"):
        a.handle("открой калькулятор")
    assert any("open_app" in r.getMessage() and "калькулятор" in r.getMessage()
               for r in caplog.records)
