"""Безопасность: аварийная остановка, подтверждения, защита от повторов и сеть.

Принципы:
- Модель никогда не исполняет код: она может только попросить вызвать
  инструмент из реестра (tool_registry), аргументы которого проверяются.
- Опасные действия выполняются только после голосового подтверждения.
- Повторяющиеся команды и слишком частые действия отбрасываются.
- Исходящие сетевые соединения разрешены только к хостам из белого списка.
"""

from __future__ import annotations

import difflib
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


# Секреты в обычную память не попадают: пароли, токены, ключи API, номера карт.
SECRET_RE = re.compile(
    r"(?:парол|password|passwd|пин-?код|\bpin\b|токен|token|api[ _-]?key|секретн|secret|"
    r"ключ api|cvv|cvc)\w*\b.*\S{4,}"
    r"|\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|xox[bp]-[A-Za-z0-9-]{10,}|"
    r"AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,})"
    r"|\b(?:\d[ -]?){13,19}\b", re.I)


def looks_secret(text: str) -> bool:
    return bool(SECRET_RE.search(text or ""))


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
    timeout: float | None = None


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

    def request(self, description: str, execute: Callable[[], Any], timeout: float | None = None) -> str:
        self.pending = PendingAction(description, execute, self.clock(), timeout)
        return (f"Требуется подтверждение: {description}. "
                f"Скажите «{self.confirm_phrases[0]}» или «{self.cancel_phrases[0]}».")

    def has_pending(self) -> bool:
        if self.pending and self.clock() - self.pending.created_at > (self.pending.timeout or self.timeout):
            self.pending = None
            return False
        return self.pending is not None

    def resolve(self, text: str) -> tuple[str, Any]:
        """Возвращает (статус, результат выполнения или None)."""
        if self.pending is None:
            return self.EXPIRED, None
        action = self.pending
        self.pending = None
        if self.clock() - action.created_at > (action.timeout or self.timeout):
            return self.EXPIRED, None
        # Подтверждение — только явная фраза; отрицание побеждает.
        if contains_phrase(text, self.cancel_phrases) and not is_exact_phrase(text, self.confirm_phrases):
            return self.CANCELLED, None
        if self._is_confirmation(text):
            return self.CONFIRMED, action.execute()
        return self.CANCELLED, None

    NEGATIONS = {"не", "нет", "ни", "отмена", "отменить", "отмени"}
    # Whisper иногда искажает «подтверждаю» («потвердая», «подтвержаю»). Похожее
    # слово принимается, только если ответ короткий (1–2 слова) и без отрицаний.
    # Порог проверен: «подтягиваю» (0.67), «подождите» (0.6) не проходят.
    FUZZY_THRESHOLD = 0.78

    def _is_confirmation(self, text: str) -> bool:
        words = normalize(text).split()
        if not words or self.NEGATIONS.intersection(words):
            return False
        if contains_phrase(text, self.confirm_phrases):
            return True
        if len(words) > 2:
            return False
        singles = [normalize(p) for p in self.confirm_phrases if " " not in normalize(p)]
        return any(difflib.SequenceMatcher(None, words[-1], p).ratio() >= self.FUZZY_THRESHOLD
                   for p in singles)

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
        self._reads: deque[float] = deque()
        self._lock = threading.Lock()  # параллельные агенты вызывают инструменты из разных потоков

    def is_duplicate_command(self, text: str) -> bool:
        """True, если та же фраза уже звучала в течение окна дублирования."""
        now = self.clock()
        norm = normalize(text)
        last = self._last_command
        self._last_command = (norm, now)
        return bool(last and last[0] == norm and now - last[1] < self.duplicate_window)

    # Чтение (поиск, страницы, файлы) безопаснее действий, но тоже ограничено — от зацикливания.
    READ_FACTOR = 5

    def allow_action(self, read_only: bool = False) -> bool:
        with self._lock:
            now = self.clock()
            bucket = self._reads if read_only else self._actions
            limit = self.max_per_minute * (self.READ_FACTOR if read_only else 1)
            while bucket and now - bucket[0] > 60:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            return True


class NetworkBlockedError(PermissionError):
    pass


class NetworkGuard:
    """Блокирует исходящие TCP/UDP-соединения к хостам вне белого списка.

    Работает на уровне socket внутри процесса помощника: случайная библиотека
    не сможет «позвонить домой». Сайты открываются в браузере отдельным
    процессом (только http/https), сам помощник страницы не загружает.
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
