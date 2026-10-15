import pytest

from jarvis.tool_registry import Param, Tool, ToolError, ToolRegistry


@pytest.fixture
def registry():
    r = ToolRegistry()
    r.register(Tool("echo", "повтор", lambda text, n=1: text * n,
                    {"text": Param("string", "текст", max_length=10),
                     "n": Param("integer", "сколько", required=False)}))
    r.register(Tool("mode", "режим", lambda m: m, {"m": Param("string", "m", enum=["a", "b"])},
                    dangerous=True, confirm_template="режим {m}"))
    return r


def test_schema_shape(registry):
    s = {x["function"]["name"]: x for x in registry.schemas()}
    assert s["echo"]["function"]["parameters"]["required"] == ["text"]
    assert s["mode"]["function"]["parameters"]["properties"]["m"]["enum"] == ["a", "b"]


def test_valid_call(registry):
    tool, args = registry.validate("echo", {"text": " hi ", "n": "2"})
    assert tool.handler(**args) == "hihi"


@pytest.mark.parametrize("name,args", [
    ("rm_rf", {}),                                  # неизвестный инструмент
    ("echo", {}),                                   # нет обязательного аргумента
    ("echo", {"text": "x", "code": "import os"}),   # лишний аргумент
    ("echo", {"text": 5}),                          # неверный тип
    ("echo", {"text": "x" * 11}),                   # слишком длинно
    ("echo", {"text": "a\nb"}),                     # управляющие символы
    ("echo", {"text": "x", "n": True}),             # bool вместо int
    ("mode", {"m": "c"}),                           # не из enum
    ("echo", "text=1"),                             # не объект
])
def test_rejects_bad_calls(registry, name, args):
    with pytest.raises(ToolError):
        registry.validate(name, args)


def test_duplicate_registration(registry):
    with pytest.raises(ValueError):
        registry.register(Tool("echo", "", lambda: ""))


def test_confirm_text(registry):
    tool, args = registry.validate("mode", {"m": "a"})
    assert registry.confirm_text(tool, args) == "режим a"
