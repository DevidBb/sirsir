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


def test_unknown_app_searched_without_llm(config):
    # «Открой …» без белого списка ищется в меню «Пуск», сайтах и папках, а не в LLM.
    a = build(config, [text("не должно понадобиться")])
    assert "Не нашёл «фотошоп»" in a.handle("открой фотошоп").text
    assert a.launched == [] and a.fake.requests == []


def test_actions_are_audited(config, caplog):
    a = build(config, [])
    with caplog.at_level("INFO", logger="jarvis.audit"):
        a.handle("открой калькулятор")
    assert any("open_app" in r.getMessage() and "калькулятор" in r.getMessage()
               for r in caplog.records)


def test_help_and_repeat(config):
    a = build(config, [text("Москва — столица России.")])
    assert a.handle("повтори").text == "Я ещё ничего не говорил."
    a.handle("какая столица России")
    assert a.handle("Повтори.").text == "Москва — столица России."
    assert a.handle("повтори").text == "Москва — столица России."  # повтор повтора
    assert "таймеры" in a.handle("Что ты умеешь?").text
    assert len(a.fake.requests) == 1


def test_repeat_only_as_whole_phrase(config):
    a = build(config, [text("Хорошо.")])
    assert a.handle("поставь таймер еще раз").text != "Я ещё ничего не говорил."


def test_streaming_speaks_sentences_as_they_come(config):
    a = build(config, [text("Чёрная дыра — это область пространства. Из неё не выходит даже свет.")])
    spoken = []
    reply = a.handle("что такое чёрная дыра", on_sentence=lambda s: spoken.append(s) or True)
    assert spoken == ["Чёрная дыра — это область пространства.", "Из неё не выходит даже свет."]
    assert reply.spoken and reply.text.startswith("Чёрная дыра")
    assert a.fake.requests[0]["stream"]


def test_streaming_interrupted(config):
    a = build(config, [text("Первое длинное предложение. Второе длинное предложение. Третье.")])
    spoken = []
    reply = a.handle("расскажи", on_sentence=lambda s: (spoken.append(s), False)[1])
    assert spoken == ["Первое длинное предложение."]
    assert reply.spoken and reply.text == ""
    assert len(a.conversation) == 0  # прерванный ответ не попадает в историю


def test_streaming_with_tool_call(config):
    a = build(config, [tool_call("open_app", name="блокнот"), text("Блокнот открыт, можно писать.")])
    spoken = []
    reply = a.handle("мне нужно что-нибудь записать, запусти редактор блокнот, пожалуйста, побыстрее",
                     on_sentence=lambda s: spoken.append(s) or True)
    assert a.launched == ["notepad.exe"]
    assert spoken == ["Блокнот открыт, можно писать."] and reply.spoken


def test_confirmation_reply_is_not_streamed(config):
    a = build(config, [tool_call("shutdown_computer")])
    spoken = []
    reply = a.handle("слушай, мне пора спать, выключи этот компьютер полностью прямо сейчас",
                     on_sentence=lambda s: spoken.append(s) or True)
    assert "подтверждение" in reply.text.lower() and not reply.spoken


def test_emergency_from_other_thread_during_answer(config):
    import threading
    a = build(config, [text("Первое длинное предложение. Второе длинное предложение. Третье предложение.")])
    spoken = []

    def on_sentence(s):
        spoken.append(s)
        # «s» в консоли / «Остановить» в трее — из другого потока, пока идёт ответ
        t = threading.Thread(target=a.emergency_stop)
        t.start()
        t.join()
        return True

    reply = a.handle("расскажи что-нибудь длинное", on_sentence=on_sentence)
    assert reply.action == "stop"
    assert spoken == ["Первое длинное предложение."]
    assert len(a.conversation) == 0
    # после остановки помощник продолжает работать
    a.fake.responses.append(text("Я здесь."))
    assert a.handle("ты тут?").text == "Я здесь."


def test_confirmation_outcome_goes_to_history(config):
    a = build(config, [tool_call("shutdown_computer"), text("ок")])
    a.registry.get("shutdown_computer").handler = lambda: "Компьютер выключится через 60 секунд."
    a.handle("слушай, выключи мне этот компьютер полностью прямо сейчас пожалуйста")
    a.handle("подтверждаю")
    a.handle("а когда он выключится")
    history = [m["content"] for m in a.fake.requests[-1]["messages"]]
    assert "Компьютер выключится через 60 секунд." in history


def test_broken_json_is_not_read_aloud(config):
    a = build(config, [text('{"name": "unknown_tool", "arguments": {}}')])
    reply = a.handle("сделай что-нибудь странное")
    assert "{" not in reply.text and "Скажите иначе" in reply.text


@pytest.mark.parametrize("phrase", ["Спасибо!", "спасибо большое", "благодарю"])
def test_thanks_ends_dialog_without_llm(config, phrase):
    a = build(config, [])
    reply = a.handle(phrase)
    assert reply.text == "Пожалуйста." and not reply.expect_followup
    assert a.fake.requests == []


@pytest.mark.parametrize("phrase", ["Стоп.", "хватит", "всё", "ничего не надо"])
def test_stop_word_after_answer_ends_dialog_silently(config, phrase):
    a = build(config, [text("Длинный ответ.")])
    a.handle("расскажи сказку")
    reply = a.handle(phrase)
    assert reply.text == "" and not reply.expect_followup and reply.action not in (STOP, EXIT)
    assert len(a.fake.requests) == 1  # в LLM не ходили
    assert a.handle("повтори").text == "Длинный ответ."  # пустой ответ не затирает последний


def test_stop_word_inside_request_still_goes_to_llm(config):
    a = build(config, [text("Хорошо.")])
    assert a.handle("хватит ли мне денег на отпуск").text == "Хорошо."
    assert len(a.fake.requests) == 1


def test_tool_returning_none_does_not_crash(config):
    from jarvis.tool_registry import Tool
    a = build(config, [tool_call("noop"), text("Сделал.")])
    a.registry.register(Tool("noop", "Ничего не делает", lambda: None))
    assert a.handle("сделай что-нибудь").text == "Сделал."
    assert a.fake.requests[1]["messages"][-1]["content"] == "Готово."



def test_hybrid_routes_simple_questions_locally_and_current_tasks_to_claude(config):
    from jarvis.assistant import Reply, is_complex_request

    config.set("llm.provider", "hybrid")
    a = build(config, [text("Локальный ответ.")])

    class FakeClaude:
        def __init__(self):
            self.calls = []

        def run(self, assistant, request, on_sentence=None):
            self.calls.append(request)
            return Reply("Ответ Claude.")

    claude = FakeClaude()
    a.agent_backend = claude

    assert a._ask_model("привет").text == "Локальный ответ."
    assert claude.calls == []

    assert is_complex_request("Какая погода завтра в Таллинне?")
    assert is_complex_request("What is tomorrow's weather in Tallinn?")
    assert is_complex_request("продолжи с предыдущего шага")
    assert a._ask_model("Какая погода завтра в Таллинне?").text == "Ответ Claude."
    assert claude.calls == ["Какая погода завтра в Таллинне?"]

    assert a._ask_model("Сравни два варианта и дай вывод.").text == "Ответ Claude."
    assert claude.calls[-1] == "Сравни два варианта и дай вывод."



def test_hybrid_keeps_quick_windows_commands_local(config):
    from jarvis.assistant import Reply

    config.set("llm.provider", "hybrid")
    a = build(config, [])

    class FakeClaude:
        calls = []

        def run(self, assistant, request, on_sentence=None):
            self.calls.append(request)
            return Reply("Ответ Claude.")

    claude = FakeClaude()
    a.agent_backend = claude
    reply = a.handle("Открой блокнот")
    assert reply.text == "Запускаю блокнот."
    assert a.launched == ["notepad.exe"]
    assert claude.calls == []
    assert a.fake.requests == []
