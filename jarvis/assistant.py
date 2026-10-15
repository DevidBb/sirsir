"""Ядро помощника: обработка одной реплики пользователя.

Не зависит от микрофона и динамиков — принимает текст, возвращает Reply.
Порядок обработки:
1. Аварийная остановка.
2. Ответ на ожидающее подтверждение.
3. Защита от повторов.
4. Встроенные голосовые команды (без LLM).
5. Диалог с LLM и вызовы инструментов из реестра.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from .config import Config
from .dictation import Dictation
from .intents import IntentMatcher
from .llm import (SYSTEM_PROMPT, Conversation, LLMError, OllamaClient, SentenceStream,
                  looks_like_tool_text)
from .memory import Memory
from .security import (CommandGuard, ConfirmationManager, EmergencyStop,
                       contains_phrase, normalize)
from .tool_registry import ToolError, ToolRegistry

log = logging.getLogger(__name__)
# Журнал выполненных действий (пишется в data/actions.log, см. main.py).
audit = logging.getLogger("jarvis.audit")

# Действия для главного цикла.
NONE, MUTE, EXIT, STOP = "none", "mute", "exit", "stop"

BUILTIN_PHRASES = {
    "cancel": ["отмена", "отмени", "отбой", "забудь", "неважно", "ничего"],
    "mute": ["выключи микрофон", "отключи микрофон", "не слушай"],
    "clear_memory": ["очисти память", "забудь все", "сотри память"],
    "reset_dialog": ["новый диалог", "сбрось диалог", "начнем сначала"],
    "exit": ["заверши работу", "выключись", "выключайся", "заверши программу"],
    "help": ["что ты умеешь", "что ты можешь", "какие команды", "помощь", "справка"],
    "repeat": ["повтори", "повтори пожалуйста", "что ты сказал", "не расслышал", "еще раз"],
    "start_dictation": ["начни диктовку", "режим диктовки", "включи диктовку", "диктовка"],
    "stop_dictation": ["закончи диктовку", "конец диктовки", "выключи диктовку", "стоп диктовка",
                       "хватит диктовки"],
}
TYPE_PREFIXES = ("напечатай ", "набери ", "впиши ")

HELP_TEXT = ("Я умею: открывать программы, сайты и папки; искать файлы; ставить таймеры и "
             "напоминания; записывать заметки; считать; управлять громкостью и музыкой; "
             "сворачивать окна; печатать под диктовку; блокировать и выключать компьютер; "
             "рассказывать о системе и просто отвечать на вопросы. "
             "Скажите «стоп», чтобы перебить меня.")


@dataclass
class Reply:
    text: str
    action: str = NONE
    # Ждать продолжения без фразы пробуждения (диалог/подтверждение).
    expect_followup: bool = True
    # Ответ уже озвучен по предложениям во время генерации (потоковый режим).
    spoken: bool = False


class Assistant:
    def __init__(self, config: Config, llm: OllamaClient, registry: ToolRegistry,
                 memory: Memory, on_setting_changed: Callable[[str, Any], None] | None = None,
                 dictation: Dictation | None = None):
        self.config = config
        self.llm = llm
        self.registry = registry
        self.memory = memory
        self.name = config.get("assistant.name", "Джарвис")
        self.max_tool_calls = int(config.get("llm.max_tool_calls_per_turn", 3))
        self.conversation = Conversation(int(config.get("assistant.max_history_turns", 6)))
        self.emergency = EmergencyStop(config.get("security.emergency_phrases", []))
        self.confirmations = ConfirmationManager(
            config.get("security.confirm_phrases", ["подтверждаю"]),
            config.get("security.cancel_phrases", ["отмена"]),
            float(config.get("security.confirmation_timeout_seconds", 20)))
        self.guard = CommandGuard(
            float(config.get("security.duplicate_command_window_seconds", 4)),
            int(config.get("security.max_actions_per_minute", 12)))
        self.on_setting_changed = on_setting_changed
        self.dictation = dictation or Dictation()
        self.intents = IntentMatcher(config.get("windows.apps", {}),
                                     config.get("windows.websites", {}),
                                     config.get("windows.folders", {}))

    # ------------------------------------------------------------------
    def emergency_stop(self) -> None:
        """Потокобезопасная остановка (из консоли или трея, пока идёт ответ):
        только флаги — историю диалога правит поток, который её ведёт."""
        self.emergency.trigger()
        self.confirmations.cancel()
        self.dictation.active = False

    def handle(self, text: str, on_sentence: Callable[[str], bool] | None = None) -> Reply | None:
        """on_sentence — озвучивать ответ LLM по мере генерации (вернуть False — прервать)."""
        reply = self._handle(text, on_sentence)
        if reply is not None and reply.text and not self._matches(text or "", "repeat"):
            self.last_reply = reply.text
        return reply

    def _handle(self, text: str, on_sentence: Callable[[str], bool] | None = None) -> Reply | None:
        text = (text or "").strip()
        if not normalize(text):
            return None

        if self.emergency.matches(text):
            self.emergency_stop()
            self.conversation.abort_turn()
            return Reply("Остановлено.", STOP, expect_followup=False)

        if self.confirmations.has_pending():
            status, result = self.confirmations.resolve(text)
            if status in (ConfirmationManager.CONFIRMED, ConfirmationManager.CANCELLED):
                answer = (str(result) if result else "Готово.") \
                    if status == ConfirmationManager.CONFIRMED else "Действие отменено."
                # Модель должна знать исход: иначе в истории остаётся «ожидает подтверждения».
                self.conversation.begin_turn(text)
                self.conversation.add({"role": "assistant", "content": answer})
                self.conversation.end_turn()
                return Reply(answer)
            # просрочено — обрабатываем как обычную реплику

        if self.dictation.active:
            return self._dictate(text)

        if self._matches(text, "repeat"):  # до антиповтора: «повтори» дважды — законно
            return Reply(getattr(self, "last_reply", "") or "Я ещё ничего не говорил.")

        if self.guard.is_duplicate_command(text):
            log.info("Повтор команды проигнорирован: %s", text)
            return None

        builtin = self._builtin(text)
        if builtin is not None:
            return builtin

        quick = self._quick_intent(text)
        if quick is not None:
            return quick

        return self._ask_llm(text, on_sentence)

    def _quick_intent(self, text: str) -> Reply | None:
        intent = self.intents.match(text)
        if intent is None or intent.tool not in self.registry.names():
            return None
        outcome = self._run_tool(intent.tool, intent.args)
        if isinstance(outcome, Reply):
            return outcome
        # Сохраняем в истории, чтобы LLM знала контекст следующих реплик.
        self.conversation.begin_turn(text)
        self.conversation.add({"role": "assistant", "content": outcome})
        self.conversation.end_turn()
        return Reply(outcome)

    # ------------------------------------------------------------------
    def _matches(self, text: str, key: str) -> bool:
        norm = normalize(text)
        phrases = BUILTIN_PHRASES[key]
        if key in ("cancel", "repeat", "help"):
            # Только как отдельная короткая реплика, иначе «поставь таймер ещё раз» → повтор.
            return norm in [normalize(p) for p in phrases]
        return contains_phrase(norm, phrases) and len(norm.split()) <= 6

    def _dictate(self, text: str) -> Reply:
        if self._matches(text, "stop_dictation"):
            self.dictation.active = False
            return Reply("Диктовка завершена.", expect_followup=False)
        error = self.dictation.type_text(text)
        if error:
            self.dictation.active = False
            return Reply(error + " Диктовка остановлена.", expect_followup=False)
        return Reply("")  # молча печатаем и слушаем дальше

    def _builtin(self, text: str) -> Reply | None:
        lowered = text.strip().lower()
        for prefix in TYPE_PREFIXES:
            if lowered.startswith(prefix):
                # Печатаем ровно то, что сказал пользователь, без участия LLM.
                error = self.dictation.type_text(text.strip()[len(prefix):])
                return Reply(error or "", expect_followup=False)
        if self._matches(text, "help"):
            return Reply(HELP_TEXT)
        if self._matches(text, "start_dictation") and not self._matches(text, "stop_dictation"):
            self.dictation.active = True
            return Reply("Диктуйте. Чтобы закончить, скажите «закончи диктовку».")
        if self._matches(text, "cancel"):
            self.conversation.abort_turn()
            return Reply("Хорошо, отменяю.", expect_followup=False)
        if self._matches(text, "mute"):
            return Reply("Выключаю микрофон. Включить снова можно клавишей Enter в окне программы.",
                         MUTE, expect_followup=False)
        if self._matches(text, "exit"):
            return Reply("Завершаю работу. До свидания.", EXIT, expect_followup=False)
        if self._matches(text, "reset_dialog"):
            self.conversation.clear()
            return Reply("Начинаем новый диалог.")
        if self._matches(text, "clear_memory"):
            def clear() -> str:
                n = self.memory.clear_facts()
                self.conversation.clear()
                return f"Память очищена, удалено записей: {n}."
            return Reply(self.confirmations.request("очистить всю долговременную память", clear))
        return None

    def _system_prompt(self) -> str:
        facts = self.memory.facts()
        block = ""
        if facts:
            block = "Что ты знаешь о пользователе:\n" + "\n".join(f"- {f}" for f in facts)
        return SYSTEM_PROMPT.format(name=self.name, facts=block)

    def _ask_llm(self, text: str, on_sentence: Callable[[str], bool] | None = None) -> Reply:
        self.emergency.reset()
        self.conversation.begin_turn(text)
        seen_calls: set[str] = set()
        spoken = False
        try:
            def guarded(sentence: str) -> bool:
                # Аварийная остановка из другого потока обрывает речь и генерацию.
                return not self.emergency.active and on_sentence(sentence) is not False

            for _ in range(self.max_tool_calls + 1):
                stream = SentenceStream(guarded) if on_sentence else None
                message = self.llm.chat(self.conversation.messages(self._system_prompt()),
                                        tools=self.registry.schemas(),
                                        on_text=stream.feed if stream else None)
                if self.emergency.active:
                    self.conversation.abort_turn()
                    return Reply("Остановлено.", STOP, expect_followup=False)
                if stream is not None:
                    if stream.cancelled:  # перебили во время ответа
                        self.conversation.abort_turn()
                        return Reply("", spoken=True)
                    stream.flush()
                    spoken = spoken or bool(stream.spoken)
                calls = message.get("tool_calls") or []
                if not calls:
                    answer = message.get("content") or ""
                    if looks_like_tool_text(answer):
                        # Похоже на сломанный вызов инструмента — не зачитываем JSON вслух.
                        log.warning("Непонятный ответ модели: %r", answer[:200])
                        self.conversation.abort_turn()
                        return Reply("Не получилось понять, что сделать. Скажите иначе, пожалуйста.")
                    self.conversation.add({"role": "assistant", "content": answer})
                    self.conversation.end_turn()
                    if not answer:
                        return Reply("Не знаю, что ответить.")
                    return Reply(answer, spoken=spoken and stream is not None and bool(stream.spoken))

                self.conversation.add({"role": "assistant", "content": message.get("content", ""),
                                       "tool_calls": calls})
                for call in calls[: self.max_tool_calls]:
                    fn = call.get("function") or {}
                    name = str(fn.get("name", ""))
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {"_invalid": args}
                    key = name + json.dumps(args, sort_keys=True, ensure_ascii=False)
                    if key in seen_calls:
                        self.conversation.abort_turn()
                        return Reply("Похоже, я зациклился. Остановлюсь.")
                    seen_calls.add(key)

                    outcome = self._run_tool(name, args)
                    if isinstance(outcome, Reply):  # нужно подтверждение
                        self.conversation.add({"role": "tool", "tool_name": name,
                                               "content": "Ожидает подтверждения пользователя."})
                        self.conversation.end_turn()
                        return outcome
                    self.conversation.add({"role": "tool", "tool_name": name, "content": outcome})
            self.conversation.abort_turn()
            return Reply("Слишком много действий подряд, остановлюсь.")
        except LLMError as e:
            log.error("LLM: %s", e)
            self.conversation.abort_turn()
            return Reply("Языковая модель недоступна. Проверьте, что Ollama запущена.")

    def _run_tool(self, name: str, args: dict) -> str | Reply:
        try:
            tool, clean = self.registry.validate(name, args)
        except ToolError as e:
            log.warning("Отклонён вызов %s(%s): %s", name, args, e)
            return f"Ошибка: {e}"
        if not self.guard.allow_action():
            return "Ошибка: слишком много действий за минуту, действие заблокировано."

        def execute() -> str:
            try:
                result = tool.handler(**clean)
            except Exception as e:  # действие не должно ронять помощника
                log.exception("Ошибка инструмента %s", name)
                result = f"Не удалось выполнить действие: {e}"
            audit.info("%s %s -> %s", name, json.dumps(clean, ensure_ascii=False), result[:200])
            return result

        if tool.dangerous:
            audit.info("%s %s -> ожидает подтверждения", name, json.dumps(clean, ensure_ascii=False))
            return Reply(self.confirmations.request(self.registry.confirm_text(tool, clean), execute))
        return execute()
