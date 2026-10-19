"""Клиент Ollama (только localhost) и ограниченная история диалога."""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Callable
from urllib.parse import urlparse

SYSTEM_PROMPT = """Ты — {name}, локальный голосовой помощник на компьютере пользователя с Windows.
Отвечай только по-русски (без иероглифов и английских вставок), коротко и по делу: обычно одно-три предложения, без markdown,
списков и эмодзи — ответ будет произнесён вслух.
Для действий на компьютере используй только предоставленные инструменты.
Ты не можешь выполнять код, команды оболочки или PowerShell — не предлагай этого.
Если подходящего инструмента нет, честно скажи, что не умеешь.
Не выдумывай результаты действий: дождись ответа инструмента.
{facts}
{now}"""


log = logging.getLogger(__name__)

MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
          "октября", "ноября", "декабря")
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")


def now_text(now: _dt.datetime | None = None) -> str:
    """Текущие дата и время для промпта: без них модель не знает, какое «завтра»."""
    now = now or _dt.datetime.now()
    tomorrow = now + _dt.timedelta(days=1)
    return (f"Сейчас {WEEKDAYS[now.weekday()]}, {now.day} {MONTHS[now.month - 1]} {now.year}, "
            f"{now:%H:%M} (время компьютера пользователя). Завтра — {tomorrow.day} "
            f"{MONTHS[tomorrow.month - 1]} {tomorrow.year}.")


class LLMError(RuntimeError):
    pass


CJK = re.compile(r"[\u2e80-\u9fff\uac00-\ud7af\uff00-\uffef]+")


def clean_markdown(text: str) -> str:
    """Убирает разметку, но не одиночные «_» (имена файлов, инструментов)."""
    text = re.sub(r"\*\*|__|`+|^\s*#+\s*|(?<!\w)\*(?!\s)|(?<!\s)\*(?!\w)", "", text, flags=re.M)
    text = CJK.sub("", text)  # qwen иногда вставляет иероглифы — TTS их не прочтёт
    return " ".join(text.split())


def strip_thinking(text: str) -> str:
    """Убирает рассуждения qwen3 (<think>…</think>) и форматирование."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"<think>.*", "", text, flags=re.S)  # незакрытый тег
    return clean_markdown(text)


def parse_text_tool_call(text: str, known: set[str] | None = None) -> list[dict] | None:
    """Модель иногда пишет вызов инструмента текстом, а не через tool_calls:
    {"name": "set_timer", "arguments": {...}} или <tool_call>…</tool_call>."""
    t = text.strip()
    m = re.search(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)", t, flags=re.S)
    if m:
        t = m.group(1)
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    if known and t.strip(".") in known:  # ответ одним именем инструмента: «system_info»
        return [{"function": {"name": t.strip("."), "arguments": {}}}]
    if not t.startswith("{"):
        return None
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("name"), str):
        return None
    args = obj.get("arguments", obj.get("parameters", {}))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return None
    if not isinstance(args, dict) or (known is not None and obj["name"] not in known):
        return None
    return [{"function": {"name": obj["name"], "arguments": args}}]


def looks_like_tool_text(text: str) -> bool:
    t = text.lstrip()
    return t.startswith(("{", "<tool_call", "```"))


class OllamaClient:
    hint = "Проверьте, что Ollama запущена."

    def __init__(self, base_url: str, model: str, temperature: float = 0.4,
                 num_predict: int = 220, num_ctx: int = 4096,
                 timeout: float = 90, keep_alive: str = "5m"):
        host = urlparse(base_url).hostname or ""
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise LLMError(f"Ollama должна работать на localhost, а не на {host}")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.num_predict = num_predict
        self.num_ctx = num_ctx
        self.timeout = timeout
        self.keep_alive = keep_alive
        self.last_stats: dict[str, Any] = {}

    def _request(self, path: str, payload: dict | None = None, timeout: float | None = None) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base_url + path, data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            raise LLMError(f"Ollama вернула ошибку {e.code}: {body}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise LLMError(f"Ollama недоступна по адресу {self.base_url}: {e}") from e

    def list_models(self) -> list[str]:
        data = self._request("/api/tags", timeout=5)
        return [m.get("name", "") for m in data.get("models", [])]

    def has_model(self) -> bool:
        names = self.list_models()
        want = self.model if ":" in self.model else self.model + ":latest"
        return want in names

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             on_text: Callable[[str], bool] | None = None,
             options: dict[str, Any] | None = None, model: str | None = None) -> dict[str, Any]:
        """Запрос к модели. С on_text ответ приходит потоком: функция получает
        каждый кусочек текста и может вернуть False, чтобы прервать генерацию.
        model — другая модель Ollama для этого запроса (например, с поддержкой изображений)."""
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "stream": on_text is not None,
            "think": False,
            "keep_alive": self.keep_alive,
            "options": {"temperature": self.temperature,
                        "num_predict": self.num_predict,
                        "num_ctx": self.num_ctx, **(options or {})},
        }
        if tools:
            payload["tools"] = tools
        if on_text is None:
            data = self._request("/api/chat", payload)
            message = data.get("message") or {}
        else:
            data, message = self._stream(payload, on_text)
        if not message.get("tool_calls") and tools:
            known = {t["function"]["name"] for t in tools if "function" in t}
            parsed = parse_text_tool_call(message.get("content") or "", known)
            if parsed:
                message["tool_calls"] = parsed
                message["content"] = ""
        # Статистика для диагностики: сколько токенов занял запрос и ответ.
        self.last_stats = {k: data.get(k) for k in
                           ("prompt_eval_count", "eval_count", "total_duration", "load_duration")}
        message["content"] = strip_thinking(message.get("content") or "")
        return message

    def _stream(self, payload: dict, on_text: Callable[[str], bool]) -> tuple[dict, dict]:
        req = urllib.request.Request(self.base_url + "/api/chat", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        content: list[str] = []
        tool_calls: list[dict] = []
        last: dict = {}
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                for raw in resp:  # одна JSON-строка на кусочек
                    if not raw.strip():
                        continue
                    last = json.loads(raw.decode("utf-8"))
                    if last.get("error"):
                        raise LLMError(f"Ollama: {last['error']}")
                    msg = last.get("message") or {}
                    tool_calls.extend(msg.get("tool_calls") or [])
                    piece = msg.get("content") or ""
                    if piece:
                        content.append(piece)
                        if on_text(piece) is False:
                            break  # пользователь перебил — дальше не генерируем
                    if last.get("done"):
                        break
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            raise LLMError(f"Ollama вернула ошибку {e.code}: {body}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise LLMError(f"Ollama недоступна по адресу {self.base_url}: {e}") from e
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return last, message

    def preload(self) -> bool:
        """Загрузить модель в память заранее, чтобы первый вопрос не ждал загрузки."""
        try:
            self._request("/api/generate", {"model": self.model, "keep_alive": self.keep_alive},
                          timeout=max(self.timeout, 120))
            return True
        except LLMError as e:
            log.warning("Предзагрузка модели не удалась: %s", e)
            return False

    def unload(self) -> None:
        """Выгрузить модель из VRAM."""
        try:
            self._request("/api/generate", {"model": self.model, "keep_alive": 0}, timeout=10)
        except LLMError:
            pass


class Conversation:
    """История диалога с ограничением по числу реплик."""

    def __init__(self, max_turns: int = 6):
        self.max_turns = max_turns
        self._turns: deque[list[dict]] = deque(maxlen=max_turns)
        self._current: list[dict] | None = None

    def begin_turn(self, user_text: str) -> None:
        self._current = [{"role": "user", "content": user_text}]

    def add(self, message: dict) -> None:
        if self._current is None:
            raise RuntimeError("begin_turn() не вызван")
        self._current.append(message)

    def end_turn(self) -> None:
        if self._current:
            self._turns.append(self._current)
        self._current = None

    def abort_turn(self) -> None:
        self._current = None

    def clear(self) -> None:
        self._turns.clear()
        self._current = None

    def messages(self, system_prompt: str) -> list[dict]:
        msgs = [{"role": "system", "content": system_prompt}]
        for turn in self._turns:
            msgs.extend(turn)
        if self._current:
            msgs.extend(self._current)
        return msgs

    def __len__(self) -> int:
        return len(self._turns)


class SentenceStream:
    """Собирает поток текста в законченные предложения для озвучивания."""

    _END = re.compile(r"[.!?…](?=[\s\"»)]|$)")

    def __init__(self, on_sentence: Callable[[str], bool], min_len: int = 12):
        self.on_sentence = on_sentence
        self.min_len = min_len
        self._buf = ""
        self.spoken: list[str] = []
        self.cancelled = False

    def _emit(self, sentence: str) -> bool:
        sentence = clean_markdown(sentence)
        if not sentence:
            return True
        self.spoken.append(sentence)
        if self.on_sentence(sentence) is False:
            self.cancelled = True
        return not self.cancelled

    def _abbreviation(self, dot: int) -> bool:
        """Точка после однобуквенного слова — «т.е.», «т.к.», инициалы «А.С.»."""
        word = re.search(r"[\w.]+$", self._buf[:dot])
        return bool(word) and len(word.group(0).replace(".", "")) <= 2 and self._buf[dot] == "."

    def feed(self, piece: str) -> bool:
        if self.cancelled:
            return False
        self._buf += piece
        if looks_like_tool_text(self._buf):
            return True  # похоже на вызов инструмента текстом — не озвучиваем
        while True:
            # Ищем конец предложения после минимальной длины, чтобы не дробить «Т.е.» и «Да.»
            m = next((m for m in self._END.finditer(self._buf)
                      if m.end() >= self.min_len and m.end() < len(self._buf)
                      and not self._abbreviation(m.start())), None)
            if m is None:
                return True
            sentence, self._buf = self._buf[:m.end()], self._buf[m.end():]
            if not self._emit(sentence):
                return False

    def flush(self) -> None:
        if self._buf.strip() and not self.cancelled and not looks_like_tool_text(self._buf):
            self._emit(self._buf)
        self._buf = ""


def find_ollama() -> str | None:
    """Путь к ollama: PATH или стандартная установка Windows."""
    import os
    import shutil
    from pathlib import Path
    found = shutil.which("ollama")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidate = Path(local) / "Programs" / "Ollama" / "ollama.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def ensure_ollama(client: OllamaClient, wait_seconds: float = 20, popen=None,
                  sleep=None) -> str:
    """Если Ollama не отвечает — запустить `ollama serve` в фоне и дождаться.
    Возвращает "running", "started" или "missing"/"failed"."""
    import subprocess
    import sys
    import time
    try:
        client.list_models()
        return "running"
    except LLMError:
        pass
    exe = find_ollama()
    if exe is None:
        return "missing"
    flags = 0
    if sys.platform == "win32":  # без окна консоли и независимо от помощника
        flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
    log.info("Ollama не запущена, запускаю: %s serve", exe)
    (popen or subprocess.Popen)([exe, "serve"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=flags)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        (sleep or time.sleep)(0.5)
        try:
            client.list_models()
            return "started"
        except LLMError:
            continue
    return "failed"
