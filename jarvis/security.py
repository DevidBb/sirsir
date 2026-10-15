"""Безопасность: аварийная остановка, подтверждения, защита от повторов и сеть.

Принципы:
- Модель никогда не исполняет код: она может только попросить вызвать
  инструмент из реестра (tool_registry), аргументы которого проверяются.
- Опасные действия выполняются только после голосового подтверждения.
- Повторяющиеся команды и слишком частые действия отбрасываются.
- Исходящие сетевые соединения разрешены только к хостам из белого списка.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable


def normalize(text: str) -> str:
    """Нижний регистр, ё→е, без пунктуации и лишних пробелов."""
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def contains_phrase(text: str, phrases: list[str]) -> bool:
    """Есть ли в тексте одна из фраз целиком (по границам слов)."""
    norm = f" {normalize(text)} "
    return any(f" {normalize(p)} " in norm for p in phrases if p.strip())


def is_exact_phrase(text: str, phrases: list[str]) -> bool:
    norm = normalize(text)
    return any(norm == normalize(p) for p in phrases)


class EmergencyStop:
    """Глобальный флаг аварийной остановки, видимый всем потокам."""

    def __init__(self, phrases: list[str]):
        self.phrases = phrases
        self._event = threading.Event()

    def matches(self, text: str) -> bool:
        return contains_phrase(text, self.phrases)

    def trigger(self) -> None:
        self._event.set()

    def reset(self) -> None:
        self._event.clear()

    @property
    def active(self) -> bool:
        return self._event.is_set()


@dataclass
class PendingAction:
    description: str
    execute: Callable[[], Any]
    created_at: float = field(default_factory=time.monotonic)


class ConfirmationManager:
    """Хранит одно ожидающее подтверждения действие.

    Пока действие ожидает, следующая реплика пользователя трактуется как
    ответ: «подтверждаю» — выполнить, «отмена» (или что угодно ещё) — отменить.
    """

    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

    def __init__(self, confirm_phrases: list[str], cancel_phrases: list[str],
                 timeout_seconds: float, clock: Callable[[], float] = time.monotonic):
        self.confirm_phrases = confirm_phrases
        self.cancel_phrases = cancel_phrases
        self.timeout = timeout_seconds
        self.clock = clock
        self.pending: PendingAction | None = None

    def request(self, description: str, execute: Callable[[], Any]) -> str:
        self.pending = PendingAction(description, execute, self.clock())
        return (f"Требуется подтверждение: {description}. "
                f"Скажите «{self.confirm_phrases[0]}» или «{self.cancel_phrases[0]}».")

    def has_pending(self) -> bool:
        if self.pending and self.clock() - self.pending.created_at > self.timeout:
            self.pending = None
            return False
        return self.pending is not None

    def resolve(self, text: str) -> tuple[str, Any]:
        """Возвращает (статус, результат выполнения или None)."""
        if self.pending is None:
            return self.EXPIRED, None
        action = self.pending
        self.pending = None
        if self.clock() - action.created_at > self.timeout:
            return self.EXPIRED, None
        # Подтверждение — только явная фраза; отрицание побеждает.
        if contains_phrase(text, self.cancel_phrases) and not is_exact_phrase(text, self.confirm_phrases):
            return self.CANCELLED, None
        if contains_phrase(text, self.confirm_phrases):
            return self.CONFIRMED, action.execute()
        return self.CANCELLED, None

    def cancel(self) -> bool:
        had = self.pending is not None
        self.pending = None
        return had


class CommandGuard:
    """Защита от случайных повторов и бесконечных циклов действий."""

    def __init__(self, duplicate_window: float, max_actions_per_minute: int,
                 clock: Callable[[], float] = time.monotonic):
        self.duplicate_window = duplicate_window
        self.max_per_minute = max_actions_per_minute
        self.clock = clock
        self._last_command: tuple[str, float] | None = None
        self._actions: deque[float] = deque()

    def is_duplicate_command(self, text: str) -> bool:
        """True, если та же фраза уже звучала в течение окна дублирования."""
        now = self.clock()
        norm = normalize(text)
        last = self._last_command
        self._last_command = (norm, now)
        return bool(last and last[0] == norm and now - last[1] < self.duplicate_window)

    def allow_action(self) -> bool:
        now = self.clock()
        while self._actions and now - self._actions[0] > 60:
            self._actions.popleft()
        if len(self._actions) >= self.max_per_minute:
            return False
        self._actions.append(now)
        return True


class NetworkBlockedError(PermissionError):
    pass


class NetworkGuard:
    """Блокирует исходящие TCP/UDP-соединения к хостам вне белого списка.

    Работает на уровне socket внутри процесса помощника: случайная библиотека
    не сможет «позвонить домой». Открытие сайта в браузере идёт отдельным
    процессом и разрешается только для сайтов из config.toml.
    """

    def __init__(self, allowed_hosts: list[str]):
        self.allowed_hosts = {h.lower() for h in allowed_hosts}
        self._orig_connect = None
        self._orig_getaddrinfo = None

    def is_allowed(self, host: Any) -> bool:
        if host is None:
            return False
        host = str(host).lower().strip("[]")
        if host in self.allowed_hosts:
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def install(self) -> None:
        if self._orig_connect is not None:
            return
        guard = self
        orig_connect = socket.socket.connect
        orig_connect_ex = socket.socket.connect_ex
        orig_getaddrinfo = socket.getaddrinfo

        def _check(sock: socket.socket, address: Any) -> None:
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                host = address[0] if isinstance(address, tuple) else address
                if not guard.is_allowed(host):
                    raise NetworkBlockedError(f"Сетевое обращение к {host} запрещено")

        def connect(sock, address):
            _check(sock, address)
            return orig_connect(sock, address)

        def connect_ex(sock, address):
            _check(sock, address)
            return orig_connect_ex(sock, address)

        def getaddrinfo(host, *args, **kwargs):
            if host is not None and not guard.is_allowed(host):
                raise NetworkBlockedError(f"Разрешение имени {host} запрещено")
            return orig_getaddrinfo(host, *args, **kwargs)

        self._orig_connect = (orig_connect, orig_connect_ex)
        self._orig_getaddrinfo = orig_getaddrinfo
        socket.socket.connect = connect
        socket.socket.connect_ex = connect_ex
        socket.getaddrinfo = getaddrinfo

    def uninstall(self) -> None:
        if self._orig_connect is None:
            return
        socket.socket.connect, socket.socket.connect_ex = self._orig_connect
        socket.getaddrinfo = self._orig_getaddrinfo
        self._orig_connect = None
        self._orig_getaddrinfo = None
