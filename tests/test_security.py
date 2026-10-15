import socket
import threading

import pytest

from jarvis.security import (CommandGuard, ConfirmationManager, EmergencyStop,
                             NetworkBlockedError, NetworkGuard, contains_phrase, normalize)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_normalize():
    assert normalize("  Джарвис, ЁЛКА!!  ") == "джарвис елка"


def test_contains_phrase_word_boundaries():
    assert contains_phrase("Джарвис, стоп стоп!", ["стоп стоп"])
    assert not contains_phrase("остановка", ["стоп"])


def test_emergency_stop():
    e = EmergencyStop(["стоп стоп"])
    assert e.matches("Стоп, стоп")
    assert not e.matches("стоп-кран")
    e.trigger()
    assert e.active
    e.reset()
    assert not e.active


def make_cm(clock):
    return ConfirmationManager(["подтверждаю"], ["отмена", "нет"], 20, clock=clock)


def test_confirmation_confirmed():
    clock = Clock()
    cm = make_cm(clock)
    calls = []
    prompt = cm.request("удалить файл", lambda: calls.append(1) or "сделано")
    assert "подтверждаю" in prompt
    assert cm.has_pending()
    status, result = cm.resolve("Подтверждаю")
    assert status == cm.CONFIRMED and result == "сделано" and calls == [1]
    assert not cm.has_pending()


@pytest.mark.parametrize("answer", ["отмена", "нет, не подтверждаю", "что там с погодой", "да"])
def test_confirmation_anything_else_cancels(answer):
    cm = make_cm(Clock())
    calls = []
    cm.request("x", lambda: calls.append(1))
    status, _ = cm.resolve(answer)
    assert status == cm.CANCELLED
    assert calls == []


def test_confirmation_expires():
    clock = Clock()
    cm = make_cm(clock)
    calls = []
    cm.request("x", lambda: calls.append(1))
    clock.t += 21
    assert not cm.has_pending()
    assert cm.resolve("подтверждаю")[0] == cm.EXPIRED
    assert calls == []


def test_duplicate_command_window():
    clock = Clock()
    g = CommandGuard(4, 100, clock=clock)
    assert not g.is_duplicate_command("открой блокнот")
    clock.t += 1
    assert g.is_duplicate_command("Открой блокнот!")
    clock.t += 5
    assert not g.is_duplicate_command("открой блокнот")


def test_rate_limit():
    clock = Clock()
    g = CommandGuard(4, 3, clock=clock)
    assert all(g.allow_action() for _ in range(3))
    assert not g.allow_action()
    clock.t += 61
    assert g.allow_action()


def test_network_guard_blocks_external_allows_localhost():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    threading.Thread(target=lambda: server.accept()[0].close(), daemon=True).start()

    guard = NetworkGuard(["127.0.0.1", "localhost"])
    guard.install()
    try:
        with pytest.raises(NetworkBlockedError):
            socket.create_connection(("example.com", 80), timeout=1)
        with pytest.raises(NetworkBlockedError):
            s = socket.socket()
            try:
                s.connect(("93.184.216.34", 80))
            finally:
                s.close()
        c = socket.create_connection(("127.0.0.1", port), timeout=2)
        c.close()
    finally:
        guard.uninstall()
        server.close()
    assert socket.socket.connect.__name__ == "connect"
    assert guard.is_allowed("::1") and guard.is_allowed("127.0.0.5")
    assert not guard.is_allowed("10.0.0.1")


@pytest.mark.parametrize("answer,ok", [
    ("ПОТВЕРДАЯ", True),            # реальное искажение Whisper
    ("Подтвержаю.", True),
    ("да, подтверждаю", True),
    ("подтягиваю", False),
    ("подождите", False),
    ("не подтверждаю", False),
    ("нет", False),
    ("я тебе потом подтвержу наверное", False),  # длинная фраза — только точное совпадение
])
def test_confirmation_tolerates_asr_errors(answer, ok):
    cm = make_cm(Clock())
    calls = []
    cm.request("x", lambda: calls.append(1))
    status, _ = cm.resolve(answer)
    assert (status == cm.CONFIRMED) == ok
    assert calls == ([1] if ok else [])
