import json
import sys
import types

import pytest

from jarvis import wake_word
from jarvis.speech_recognition import is_hallucination
from jarvis.wake_word import VoskWakeWord, strip_wake_phrase


@pytest.mark.parametrize("text", [
    "", "  ", "Продолжение следует...", "Субтитры сделал DimaTorzok",
    "Редактор субтитров А.Семкин Корректор А.Егорова", "Спасибо за просмотр!", "Угу.",
])
def test_hallucinations_dropped(text):
    assert is_hallucination(text)


@pytest.mark.parametrize("text", [
    "открой блокнот", "Спасибо, Джарвис", "Поставь таймер на 5 минут.", "Подтверждаю.", "да",
])
def test_real_commands_kept(text):
    assert not is_hallucination(text)


@pytest.mark.parametrize("text,out", [
    ("Джарвис, открой блокнот", "открой блокнот"),
    ("Джервис открой блокнот.", "открой блокнот."),     # так Whisper пишет без подсказки
    ("Jarvis, который час?", "который час?"),
    ("Эй, Джарвис, сделай потише", "сделай потише"),
    ("Жарвис стоп", "стоп"),
    ("Дарвин был учёным", "Дарвин был учёным"),        # похоже, но не обращение
    ("открой блокнот", "открой блокнот"),
    ("Джарвис", ""),
    ("Привет, Джарвис, открой ютуб", "открой ютуб"),
    ("привет джарвис открой ютуб", "открой ютуб"),
    ("Доброе утро, Джарвис, который час?", "который час?"),
    ("Скажи Джарвису привет", "Скажи Джарвису привет"),  # обращение не в начале — не трогаем
])
def test_strip_wake_phrase_fuzzy(text, out):
    assert strip_wake_phrase(text, ["джарвис"]) == out


class FakeModel:
    VOCAB = {"джарвис", "стоп", "париж", "дом", "хватит"}

    def vosk_model_find_word(self, word):
        return 1 if word in self.VOCAB else -1


class FakeRecognizer:
    def __init__(self, model, sr, grammar):
        self.grammar = json.loads(grammar)


@pytest.fixture
def fake_vosk(monkeypatch):
    monkeypatch.setitem(sys.modules, "vosk", types.SimpleNamespace(KaldiRecognizer=FakeRecognizer))
    monkeypatch.setattr(wake_word, "DISTRACTORS", ["париж", "дом", "стоп", "неизвестное"])


def make(phrases):
    w = VoskWakeWord("x", phrases)
    w._model = FakeModel()
    w._build_grammar()
    w.reset()
    return w


def test_grammar_has_distractors_and_skips_unknown(fake_vosk):
    w = make(["джарвис", "джарвиз"])
    assert w.active_phrases == ["джарвис"]
    g = w._rec.grammar
    assert g[0] == "джарвис" and g[-1] == "[unk]"
    assert "париж" in g and "дом" in g and "неизвестное" not in g


def test_trigger_words_not_used_as_distractors(fake_vosk):
    w = make(["стоп", "хватит", "джарвис стоп"])
    distractors = w._rec.grammar[len(w.active_phrases):-1]
    assert "стоп" not in distractors and "джарвис" not in distractors


def test_no_usable_phrase_is_an_error(fake_vosk):
    with pytest.raises(ValueError):
        make(["джарвиз"])


def test_nvidia_dll_dirs(tmp_path, monkeypatch):
    import os
    from jarvis import speech_recognition as sr
    for pkg in ("cublas", "cudnn"):
        (tmp_path / "nvidia" / pkg / "bin").mkdir(parents=True)
    added_dirs = []
    monkeypatch.setattr(sr.sys, "platform", "win32")
    monkeypatch.setattr(sr.sys, "path", [str(tmp_path)])
    monkeypatch.setattr(os, "add_dll_directory", added_dirs.append, raising=False)
    monkeypatch.setenv("PATH", "orig")
    added = sr.add_nvidia_dll_dirs()
    assert len(added) == 2 and sorted(added) == sorted(added_dirs)
    assert os.environ["PATH"].endswith("orig") and "cudnn" in os.environ["PATH"]


def test_nvidia_dll_dirs_noop_elsewhere(monkeypatch):
    from jarvis import speech_recognition as sr
    monkeypatch.setattr(sr.sys, "platform", "linux")
    assert sr.add_nvidia_dll_dirs() == []
