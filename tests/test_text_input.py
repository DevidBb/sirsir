"""«Пиши там всё»: печать в нужное окно, текст от LLM по подтверждению, сочетания клавиш.
Windows API (SendInput, окна) подменены фейками."""

import pytest

from conftest import FakeLLM, text
from jarvis.assistant import BUILTIN_PHRASES, Assistant
from jarvis.dictation import HOTKEYS, VK_CONTROL, VK_RETURN, VK_SHIFT, Dictation, prepare_text
from jarvis.intents import Intent, IntentMatcher
from jarvis.memory import Memory
from jarvis.security import normalize
from jarvis.tool_registry import ToolRegistry


class FakeWindows:
    available = True

    def __init__(self, titles, desktop):
        self._titles = titles
        self.desktop = desktop   # общий «рабочий стол»: какое окно активно
        self.focused = []

    def titles(self):
        return list(self._titles)

    def act(self, query, action):
        assert action == "focus"
        for t in self._titles:
            if normalize(query) in normalize(t):
                self.focused.append(t)
                if self.desktop.get("focus_works", True):
                    self.desktop["active"] = (self.desktop["classes"].get(t, "Window"), t)
                return t
        return None


TITLES = ["Telegram", "Безымянный — Блокнот", "Новая вкладка — Google Chrome",
          "Windows PowerShell", "Документ1 - Word"]


def make(config, responses=(), active=("Notepad", "Безымянный — Блокнот"), focus_works=True):
    desktop = {"active": active, "focus_works": focus_works,
               "classes": {"Windows PowerShell": "ConsoleWindowClass"}}
    typed, keys = [], []
    windows = FakeWindows(TITLES, desktop)
    d = Dictation(typer=typed.append, foreground=lambda: desktop["active"],
                  key_sender=keys.append, windows=windows, sleep=lambda s: None)
    llm = FakeLLM(list(responses))
    a = Assistant(config, llm, ToolRegistry(), Memory(config.path("memory.db_path")), dictation=d)
    a.typed, a.keys, a.windows, a.desktop, a.fake = typed, keys, windows, desktop, llm
    return a


# --- фразы ---------------------------------------------------------------
@pytest.mark.parametrize("phrase,intent", [
    ("Новая строка", Intent("press_keys", {"keys": "new_line"})),
    ("сохрани", Intent("press_keys", {"keys": "save"})),
    ("отмени ввод", Intent("press_keys", {"keys": "undo"})),
    ("Верни как было.", Intent("press_keys", {"keys": "undo"})),
    ("выдели всё", Intent("press_keys", {"keys": "select_all"})),
    ("копировать", Intent("press_keys", {"keys": "copy"})),
    ("вставить", Intent("press_keys", {"keys": "paste"})),
    ("закрой вкладку", Intent("press_keys", {"keys": "close_tab"})),
    ("новая вкладка", Intent("press_keys", {"keys": "new_tab"})),
    ("отправь", Intent("send_message")),
    ("Напиши в телеграм: привет, как дела?",
     Intent("type_in_window", {"window": "телеграм", "text": "привет, как дела?"})),
    ("напиши в окно блокнот купить хлеб",
     Intent("type_in_window", {"window": "блокнот", "text": "купить хлеб"})),
    ("Напиши письмо начальнику, что я заболел",
     Intent("compose_text", {"request": "Напиши письмо начальнику, что я заболел"})),
    ("сочини поздравление маме с днём рождения",
     Intent("compose_text", {"request": "сочини поздравление маме с днём рождения"})),
])
def test_input_intents(phrase, intent):
    assert IntentMatcher({}, {}).match(phrase) == intent


@pytest.mark.parametrize("phrase", ["сохрани мой номер телефона 123", "отправь письмо маме завтра",
                                    "напиши", "напомни через 5 минут вставить хлеб"])
def test_input_intents_not_overmatched(phrase):
    intent = IntentMatcher({}, {}).match(phrase)
    assert intent is None or intent.tool not in ("press_keys", "send_message")


def test_undo_phrase_does_not_conflict_with_cancel():
    """«Отмена»/«отмени» — встроенная отмена диалога; Ctrl+Z — другими словами."""
    cancel = {normalize(p) for p in BUILTIN_PHRASES["cancel"]}
    from jarvis.intents import HOTKEY_PHRASES
    for phrases in HOTKEY_PHRASES.values():
        assert not cancel.intersection(normalize(p) for p in phrases)


def test_cancel_still_cancels_and_undo_presses_ctrl_z(config):
    a = make(config)
    assert a.handle("отмени").text == "Хорошо, отменяю."
    assert a.handle("отмена").text == "Хорошо, отменяю."
    assert a.keys == []
    a.handle("отмени ввод")
    assert a.keys == [[VK_CONTROL, ord("Z")]]
    assert a.fake.requests == []


# --- сочетания клавиш ----------------------------------------------------
def test_hotkeys_pressed(config):
    a = make(config)
    for phrase in ("выдели всё", "копировать", "новая вкладка", "вставить", "сохрани",
                   "новая строка", "закрой вкладку"):
        reply = a.handle(phrase)
        assert reply.text == "" and reply.expect_followup
    assert a.keys == [HOTKEYS[k][1] for k in
                      ("select_all", "copy", "new_tab", "paste", "save", "new_line", "close_tab")]
    assert [VK_RETURN] not in a.keys            # «новая строка» — Shift+Enter, не отправка
    assert HOTKEYS["new_line"][1] == [VK_SHIFT, VK_RETURN]


def test_hotkeys_refused_in_terminal(config):
    a = make(config, active=("CASCADIA_HOSTING_WINDOW_CLASS", "Windows PowerShell"))
    assert "терминала" in a.handle("вставить").text
    assert "терминала" in a.handle("закрой вкладку").text
    assert a.keys == []


def test_press_rejects_unknown_combo(config):
    a = make(config)
    assert "не разрешено" in a.dictation.press("alt_f4")
    assert a.keys == []


def test_send_requires_confirmation(config):
    a = make(config)
    reply = a.handle("отправь")
    assert "Требуется подтверждение" in reply.text and a.keys == []
    assert a.handle("подтверждаю").text == "Отправил."
    assert a.keys == [[VK_RETURN]]
    a.handle("отправь сообщение")
    assert a.handle("нет").text == "Действие отменено."
    assert a.keys == [[VK_RETURN]]


def test_send_refused_in_terminal_even_after_confirmation(config):
    a = make(config)
    a.handle("отправь")
    a.desktop["active"] = ("ConsoleWindowClass", "cmd.exe")
    assert "терминала" in a.handle("подтверждаю").text
    assert a.keys == []


def test_new_line_in_dictation_presses_shift_enter(config):
    a = make(config)
    a.handle("начни диктовку")
    a.handle("первая строка")
    a.handle("новая строка")
    a.handle("вторая")
    assert a.typed == ["первая строка ", "вторая "]
    assert a.keys == [[VK_SHIFT, VK_RETURN]]


# --- «напиши в <окно> <текст>» ------------------------------------------
def test_type_into_window(config):
    a = make(config, active=("Chrome_WidgetWin_1", "Новая вкладка — Google Chrome"))
    reply = a.handle("Напиши в телеграм: привет, как дела?")
    assert reply.text == ""
    assert a.windows.focused == ["Telegram"]
    assert a.typed == ["привет, как дела? "]


def test_type_into_two_word_window(config):
    a = make(config)
    a.handle("напиши в google chrome погода в москве")
    assert a.windows.focused == ["Новая вкладка — Google Chrome"]
    assert a.typed == ["погода в москве "]


def test_type_into_russian_alias(config):
    a = make(config)
    a.handle("напиши в ворд отчёт готов")
    assert a.windows.focused == ["Документ1 - Word"] and a.typed == ["отчёт готов "]


def test_type_into_missing_window(config):
    a = make(config)
    assert "не найдено" in a.handle("напиши в дискорд привет").text
    assert a.typed == []


def test_type_into_terminal_refused(config):
    a = make(config)
    assert "терминала" in a.handle("напиши в powershell del *").text
    assert a.windows.focused == [] and a.typed == []


def test_type_into_not_when_focus_failed(config):
    a = make(config, focus_works=False)
    assert "печатать не стал" in a.handle("напиши в телеграм привет").text
    assert a.typed == []


# --- текст от LLM ---------------------------------------------------------
LETTER = ("Здравствуйте, Иван Петрович! К сожалению, я заболел и сегодня не смогу выйти на работу. "
          "С точки зрения сроков всё под контролем. Буду на связи.")


def test_compose_types_only_after_confirmation(config):
    a = make(config, [text(LETTER)])
    reply = a.handle("Напиши письмо начальнику, что я заболел")
    assert reply.text.startswith("Вот что получилось: Здравствуйте, Иван Петрович!")
    assert "Требуется подтверждение" in reply.text and "…" in reply.text
    assert a.typed == []
    req = a.fake.requests[0]
    assert req["tools"] is None                          # модель только пишет текст
    assert req["messages"][-1]["content"] == "Напиши письмо начальнику, что я заболел"
    assert req["options"]["num_predict"] >= 300
    assert a.handle("подтверждаю").text == "Напечатал."
    # Готовый текст печатается как есть: «точки зрения» не превращается в «.».
    assert a.typed == [LETTER + " "]


def test_compose_cancel(config):
    a = make(config, [text("Привет! Скоро буду.")])
    a.handle("напиши сообщение другу, что я опаздываю")
    assert a.handle("отмена").text == "Действие отменено."
    assert a.typed == []


def test_compose_strips_control_chars_and_never_presses_enter(config):
    a = make(config, [text("Строка один\nСтрока два\r\n\x1b[31m")])
    a.handle("напиши текст для поста")
    a.handle("подтверждаю")
    assert all(ch >= " " for ch in a.typed[0])
    assert a.keys == []


def test_compose_refused_in_terminal(config):
    a = make(config, [text("echo hacked")], active=("ConsoleWindowClass", "cmd.exe"))
    a.handle("напиши текст")
    assert "терминала" in a.handle("подтверждаю").text
    assert a.typed == []


def test_compose_llm_down(config):
    from jarvis.llm import LLMError
    a = make(config, [LLMError("нет связи")])
    assert "недоступна" in a.handle("напиши письмо маме").text
    assert not a.confirmations.has_pending()


def test_prepare_text_without_voice_punctuation():
    assert prepare_text("С точки зрения запятая всё", voice_punctuation=False) == \
        "С точки зрения запятая всё "


def test_prompt_requests_are_composed_as_prompts(config):
    from jarvis.assistant import compose_system_prompt
    from jarvis.intents import IntentMatcher

    matcher = IntentMatcher({}, {})
    phrases = [
        "Напиши промпт для ChatGPT, чтобы он сделал сайт",
        "Создай инструкцию для Claude Code",
        "Сгенерируй prompt для ИИ",
    ]
    for phrase in phrases:
        intent = matcher.match(phrase)
        assert intent is not None and intent.tool == "compose_text"
        assert "Не ограничивайся 80 словами" in compose_system_prompt(phrase)

    assert "до 80 слов" in compose_system_prompt("Напиши письмо начальнику")



def test_hybrid_prompt_uses_claude_without_tools_and_targets_chatgpt(config):
    from jarvis.assistant import Reply

    a = make(config, [text("Эта локальная генерация не должна вызываться.")])
    a.config.set("llm.provider", "hybrid")
    a.windows._titles = list(a.windows._titles) + ["ChatGPT — Google Chrome"]

    class FakeClaude:
        def __init__(self):
            self.calls = []
            self.session_id = "user-session"

        def run(self, isolated, request, on_sentence=None):
            self.calls.append(request)
            assert self.session_id is None  # промпт — в отдельной сессии Claude
            self.session_id = "compose-session"
            assert isolated.turn_tools() == []
            assert "готовый к копированию" in isolated.skill_prompt()
            return Reply("Ты — эксперт по созданию сайтов. Подготовь подробный план и код.")

    claude = FakeClaude()
    a.agent_backend = claude
    draft = "Ты — эксперт по созданию сайтов. Подготовь подробный план и код."
    reply = a._compose("Напиши промпт для ChatGPT, чтобы он создал сайт.", target_window="ChatGPT")

    assert "Требуется подтверждение" in reply.text
    assert claude.calls == ["Напиши промпт для ChatGPT, чтобы он создал сайт."]
    assert claude.session_id == "user-session"  # разговор пользователя с Claude не потерян
    assert a.fake.requests == []

    result = a.handle("подтверждаю")
    assert result.text == "Напечатал."
    assert a.typed == [draft + " "]
    assert a.windows.focused == ["ChatGPT — Google Chrome"]


# --- чат HUD: активное окно — сам HUD, печатать туда нельзя ----------------
def test_hud_compose_shows_full_text_with_copy_and_types_nothing(config):
    a = make(config, [text(LETTER)], active=("Chrome_WidgetWin_1", "J.A.R.V.I.S. — Google Chrome"))
    reply = a.handle("Напиши письмо начальнику, что я заболел", channel="hud")
    assert reply.data == {"kind": "draft", "text": LETTER}
    assert LETTER in reply.text and "Скопируйте" in reply.text
    assert not a.confirmations.has_pending()
    assert a.typed == [] and a.keys == []


@pytest.mark.parametrize("phrase", ["выдели всё", "отправь", "напечатай привет", "начни диктовку"])
def test_hud_keyboard_commands_explain_instead_of_typing_into_hud(config, phrase):
    a = make(config, active=("Chrome_WidgetWin_1", "J.A.R.V.I.S. — Google Chrome"))
    reply = a.handle(phrase, channel="hud")
    assert "голосом" in reply.text
    assert a.typed == [] and a.keys == [] and not a.dictation.active
    assert not a.confirmations.has_pending()


def test_voice_channel_keeps_typing_behaviour(config):
    a = make(config)
    a.handle("выдели всё")
    assert a.keys == [HOTKEYS["select_all"][1]]
