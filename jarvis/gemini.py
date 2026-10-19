"""Клиент Gemini (облако Google) с тем же интерфейсом, что у ``OllamaClient``.

Используется OpenAI-совместимый API Gemini, поэтому цикл инструментов, подтверждения и
журнал в ``Assistant._ask_llm`` работают так же, как с локальной моделью. Ключ — переменная
``GEMINI_API_KEY`` (в .env). Запросы и результаты инструментов уходят в интернет.

Gemini 3 требует вернуть вместе с вызовом инструмента его «подпись мыслей»
(``extra_content``) — поэтому вызовы хранятся в истории в исходном виде.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from .llm import LLMError, parse_text_tool_call, strip_thinking

log = logging.getLogger(__name__)

GEMINI_HOST = "generativelanguage.googleapis.com"
CHAT_URL = f"https://{GEMINI_HOST}/v1beta/openai/chat/completions"
KEY_ENV = "GEMINI_API_KEY"
# Временные сбои Google («high demand») — один повтор через паузу, затем запасная модель.
RETRY_CODES = (500, 502, 503, 504)
DEFAULT_MODEL = "gemini-3.6-flash"
FALLBACK_MODEL = "gemini-3.5-flash"


class GeminiBusy(LLMError):
    """Модель перегружена или не ответила вовремя — имеет смысл спросить другую."""


def api_key() -> str:
    return os.environ.get(KEY_ENV, "").strip()


def _image_part(data: str) -> dict:
    mime = "image/png" if data.startswith("iVBOR") else "image/jpeg"
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}


def to_openai(messages: list[dict]) -> list[dict]:
    """История в формате Ollama → формат OpenAI.

    Ответы инструментов у Ollama идут по порядку без id — сопоставляем их с вызовами
    предыдущего сообщения модели. Вызовы без ответа (например, ждущие подтверждения
    следом за другим) отбрасываются: API требует ответ на каждый вызов."""
    out: list[dict] = []
    pending: list[str] = []
    answered = 0
    calls_msg: dict | None = None
    for m in messages:
        role = m.get("role")
        if role != "tool" and calls_msg is not None:
            calls_msg["tool_calls"] = calls_msg["tool_calls"][:answered]
            if not calls_msg["tool_calls"]:
                del calls_msg["tool_calls"]
            calls_msg = None
        if role == "tool":
            call_id = pending.pop(0) if pending else f"call_{len(out)}"
            answered += 1
            out.append({"role": "tool", "tool_call_id": call_id, "content": str(m.get("content", ""))})
        elif role == "assistant" and m.get("tool_calls"):
            calls = []
            for i, c in enumerate(m["tool_calls"]):
                fn = c.get("function") or {}
                args = fn.get("arguments") or {}
                call = {"id": c.get("id") or f"call_{len(out)}_{i}", "type": "function",
                        "function": {"name": fn.get("name", ""),
                                     "arguments": args if isinstance(args, str)
                                     else json.dumps(args, ensure_ascii=False)}}
                if c.get("extra_content"):
                    call["extra_content"] = c["extra_content"]
                calls.append(call)
            pending, answered = [c["id"] for c in calls], 0
            calls_msg = {"role": "assistant", "content": m.get("content") or None, "tool_calls": calls}
            out.append(calls_msg)
        elif m.get("images"):
            out.append({"role": role, "content": [{"type": "text", "text": m.get("content", "")},
                                                  *(_image_part(d) for d in m["images"])]})
        else:
            out.append({"role": role, "content": m.get("content", "")})
    if calls_msg is not None:
        calls_msg["tool_calls"] = calls_msg["tool_calls"][:answered]
        if not calls_msg["tool_calls"]:
            del calls_msg["tool_calls"]
    return out


def _parse_calls(raw: list[dict]) -> list[dict]:
    """Вызовы из ответа → формат цикла Джарвиса (аргументы — словарь), id и подпись сохраняются."""
    calls = []
    for c in raw:
        fn = c.get("function") or {}
        args: Any = fn.get("arguments") or "{}"
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except json.JSONDecodeError:
                pass  # Assistant отметит как _invalid
        call = {"id": c.get("id"), "function": {"name": fn.get("name", ""), "arguments": args}}
        if c.get("extra_content"):
            call["extra_content"] = c["extra_content"]
        calls.append(call)
    return calls


class GeminiClient:
    hint = "Проверьте ключ GEMINI_API_KEY в .env и интернет."

    def __init__(self, model: str = DEFAULT_MODEL, reasoning_effort: str = "low",
                 timeout: float = 30, key: str | None = None, retry_pause: float = 1.5,
                 url: str = CHAT_URL, fallback_model: str = FALLBACK_MODEL):
        self.model = model
        self.fallback_model = fallback_model
        self.url = url
        self.reasoning_effort = reasoning_effort
        self.timeout = timeout
        self._key = key
        self.retry_pause = retry_pause
        # Совместимость с OllamaClient: эти настройки Gemini не использует (длину ответа
        # задаёт промпт, а max_tokens у Gemini включает размышления и обрезал бы ответ).
        self.num_predict = 0
        self.temperature = None
        self.vision_model = model  # Gemini понимает изображения сама
        self.last_stats: dict[str, Any] = {}

    @property
    def key(self) -> str:
        return self._key if self._key is not None else api_key()

    def _send(self, payload: dict):
        """Запрос; перегруженная модель — запасная (поток ещё не начался, ответ не потерян)."""
        try:
            return self._open(payload)
        except GeminiBusy as e:
            if not self.fallback_model or payload["model"] == self.fallback_model:
                raise
            log.warning("%s (%s) — спрашиваю %s", e, payload["model"], self.fallback_model)
            return self._open({**payload, "model": self.fallback_model})

    def _open(self, payload: dict):
        if not self.key:
            raise LLMError(f"Не задан ключ Gemini: добавьте {KEY_ENV}=... в файл .env")
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + self.key},
                                     method="POST")
        for attempt in (1, 2):
            try:
                return urllib.request.urlopen(req, timeout=self.timeout)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace").replace(self.key, "***")
                if e.code in RETRY_CODES and attempt == 1:
                    log.warning("Gemini %s, повторяю: %s", e.code, body[:200])
                    time.sleep(self.retry_pause)
                    continue
                error = GeminiBusy if e.code in RETRY_CODES else LLMError
                raise error(self._http_error(e.code, body)) from e
            except TimeoutError as e:
                raise GeminiBusy(f"Gemini не ответил за {self.timeout:.0f} с") from e
            except (urllib.error.URLError, OSError) as e:
                if isinstance(getattr(e, "reason", None), TimeoutError):
                    raise GeminiBusy(f"Gemini не ответил за {self.timeout:.0f} с") from e
                raise LLMError(f"Gemini недоступен: {e}") from e
        raise LLMError("Gemini недоступен")

    @staticmethod
    def _http_error(code: int, body: str) -> str:
        try:
            data = json.loads(body)
            data = data[0] if isinstance(data, list) and data else data
            message = str(data.get("error", {}).get("message") or body)
        except (ValueError, AttributeError):
            message = body
        message = message[:300]
        if code in (401, 403) or "API key" in message:
            return f"Gemini не принял ключ ({code}): {message}"
        if code == 429:
            return f"Исчерпан лимит Gemini (бесплатный тариф) — попробуйте позже: {message}"
        return f"Gemini вернул ошибку {code}: {message}"

    def list_models(self) -> list[str]:
        return [self.model] if self.key else []

    def has_model(self) -> bool:
        return bool(self.key)

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             on_text: Callable[[str], bool] | None = None,
             options: dict[str, Any] | None = None, model: str | None = None) -> dict[str, Any]:
        """Тот же контракт, что у OllamaClient.chat; options (настройки Ollama) не используются."""
        payload: dict[str, Any] = {"model": model or self.model, "messages": to_openai(messages),
                                   "stream": on_text is not None}
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        if tools:
            payload["tools"] = tools
        if on_text is None:
            with self._send(payload) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            choice = (data.get("choices") or [{}])[0]
            raw = choice.get("message") or {}
            message: dict[str, Any] = {"role": "assistant", "content": raw.get("content") or ""}
            if raw.get("tool_calls"):
                message["tool_calls"] = _parse_calls(raw["tool_calls"])
        else:
            data, message = self._stream(payload, on_text)
        if not message.get("tool_calls") and tools:
            known = {t["function"]["name"] for t in tools if "function" in t}
            parsed = parse_text_tool_call(message.get("content") or "", known)
            if parsed:
                message["tool_calls"] = parsed
                message["content"] = ""
        usage = data.get("usage") or {}
        self.last_stats = {"prompt_eval_count": usage.get("prompt_tokens"),
                           "eval_count": usage.get("completion_tokens")}
        message["content"] = strip_thinking(message.get("content") or "")
        return message

    def _stream(self, payload: dict, on_text: Callable[[str], bool]) -> tuple[dict, dict]:
        content: list[str] = []
        calls: dict[int, dict] = {}
        last: dict = {}
        with self._send(payload) as resp:
            for raw in resp:  # Server-Sent Events: «data: {...}»
                line = raw.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                last = json.loads(chunk)
                if last.get("error"):
                    raise LLMError(f"Gemini: {last['error']}")
                delta = ((last.get("choices") or [{}])[0]).get("delta") or {}
                for i, c in enumerate(delta.get("tool_calls") or []):
                    acc = calls.setdefault(c.get("index", i), {"function": {"name": "", "arguments": ""}})
                    if c.get("id"):
                        acc["id"] = c["id"]
                    if c.get("extra_content"):
                        acc["extra_content"] = c["extra_content"]
                    fn = c.get("function") or {}
                    acc["function"]["name"] += fn.get("name") or ""
                    acc["function"]["arguments"] += fn.get("arguments") or ""
                piece = delta.get("content") or ""
                if piece:
                    content.append(piece)
                    if on_text(piece) is False:
                        break  # пользователь перебил — дальше не читаем
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if calls:
            message["tool_calls"] = _parse_calls([calls[i] for i in sorted(calls)])
        return last, message

    def preload(self) -> bool:
        return True  # облачной модели нечего загружать

    def unload(self) -> None:
        pass


def make_llm(config):
    """Клиент модели для цикла инструментов: Gemini или локальная Ollama."""
    from .llm import OllamaClient
    if str(config.get("llm.provider", "ollama")).lower() == "gemini":
        return GeminiClient(model=str(config.get("llm.gemini.model", "") or DEFAULT_MODEL),
                            fallback_model=str(config.get("llm.gemini.fallback_model", FALLBACK_MODEL) or ""),
                            reasoning_effort=str(config.get("llm.gemini.reasoning_effort", "low") or ""),
                            timeout=float(config.get("llm.gemini.timeout_seconds", 30)))
    return OllamaClient(
        config.get("llm.base_url"), config.get("llm.model"),
        temperature=float(config.get("llm.temperature", 0.4)),
        num_predict=int(config.get("llm.num_predict", 220)),
        num_ctx=int(config.get("llm.num_ctx", 8192)),
        timeout=float(config.get("llm.timeout_seconds", 90)),
        keep_alive=str(config.get("llm.keep_alive", "30m")))
