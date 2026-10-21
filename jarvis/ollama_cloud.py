"""Ollama Cloud через официальный HTTPS API; локальный клиент остаётся только на localhost."""

from __future__ import annotations

import os
from typing import Any

from .llm import LLMError, OllamaClient

HOST = "ollama.com"
BASE_URL = f"https://{HOST}"
KEY_ENV = "OLLAMA_API_KEY"
DEFAULT_MODEL = "gemma4:31b"


def api_key() -> str:
    return os.environ.get(KEY_ENV, "").strip()


class OllamaCloudClient(OllamaClient):
    hint = "Проверьте OLLAMA_API_KEY в .env и интернет."

    def __init__(self, model: str = DEFAULT_MODEL, timeout: float = 90,
                 num_predict: int = 900, temperature: float = 0.4, key: str | None = None):
        super().__init__("http://127.0.0.1:11434", model, temperature=temperature,
                         num_predict=num_predict, timeout=timeout)
        self.base_url = BASE_URL  # адрес фиксирован: ключ никогда не уходит на произвольный хост
        self._key = key

    @property
    def key(self) -> str:
        return self._key if self._key is not None else api_key()

    def _headers(self) -> dict[str, str]:
        if not self.key:
            raise LLMError(f"Нет ключа Ollama Cloud: добавьте {KEY_ENV}=... в .env")
        return {**super()._headers(), "Authorization": "Bearer " + self.key}

    def _http_error(self, code: int, body: str) -> str:
        body = body.replace(self.key, "***") if self.key else body
        if code in (401, 403):
            return "Ollama Cloud: ключ не принят. Проверьте OLLAMA_API_KEY в .env."
        if code == 429:
            return "Ollama Cloud: достигнут лимит запросов. Попробуйте позже."
        return f"Ollama Cloud вернула ошибку {code}: {body[:250]}"

    def _prepare_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Контекст и время хранения в VRAM управляются сервером; think зависит от модели.
        payload.pop("keep_alive", None)
        payload.pop("think", None)
        payload["options"].pop("num_ctx", None)
        return payload

    def preload(self) -> bool:
        return True

    def unload(self) -> None:
        pass


def from_config(config: Any, timeout: float | None = None) -> OllamaCloudClient:
    return OllamaCloudClient(
        model=str(config.get("llm.ollama_cloud.model", "") or DEFAULT_MODEL),
        timeout=timeout if timeout is not None else float(config.get("llm.ollama_cloud.timeout_seconds", 90)),
        num_predict=int(config.get("llm.num_predict", 220)),
        temperature=float(config.get("llm.temperature", 0.4)))
