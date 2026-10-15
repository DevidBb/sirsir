"""Клиент Ollama (только localhost) и ограниченная история диалога."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections import deque
from typing import Any
from urllib.parse import urlparse

SYSTEM_PROMPT = """Ты — {name}, локальный голосовой помощник на компьютере пользователя с Windows.
Отвечай по-русски, коротко и по делу: обычно одно-три предложения, без markdown,
списков и эмодзи — ответ будет произнесён вслух.
Для действий на компьютере используй только предоставленные инструменты.
Ты не можешь выполнять код, команды оболочки или PowerShell — не предлагай этого.
Если подходящего инструмента нет, честно скажи, что не умеешь.
Не выдумывай результаты действий: дождись ответа инструмента.
{facts}"""


class LLMError(RuntimeError):
    pass


def strip_thinking(text: str) -> str:
    """Убирает рассуждения qwen3 (<think>…</think>) и форматирование."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"<think>.*", "", text, flags=re.S)  # незакрытый тег
    text = re.sub(r"[*_#`]+", "", text)
    return " ".join(text.split())


class OllamaClient:
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

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "think": False,
            "keep_alive": self.keep_alive,
            "options": {"temperature": self.temperature,
                        "num_predict": self.num_predict,
                        "num_ctx": self.num_ctx},
        }
        if tools:
            payload["tools"] = tools
        data = self._request("/api/chat", payload)
        message = data.get("message") or {}
        message["content"] = strip_thinking(message.get("content") or "")
        return message

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
