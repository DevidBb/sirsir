"""Реестр разрешённых действий.

LLM видит только описания инструментов отсюда и может лишь попросить вызвать
один из них с аргументами. Неизвестные инструменты, лишние или неверные
аргументы отклоняются. Произвольный код не исполняется никогда.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Param:
    type: str  # "string" | "integer" | "boolean"
    description: str
    required: bool = True
    enum: list[str] | None = None
    max_length: int = 200


@dataclass
class Tool:
    name: str
    description: str
    handler: Callable[..., str]
    params: dict[str, Param] = field(default_factory=dict)
    # Опасные действия требуют голосового подтверждения.
    dangerous: bool = False
    # Текст для подтверждения, например "удалить файл {path}".
    confirm_template: str = ""
    # Подтверждение в зависимости от аргументов: вернуть текст «что сделать» или None.
    # Например, клик по кнопке «Купить» требует подтверждения, по ссылке — нет.
    confirm_check: Callable[[dict[str, Any]], str | None] | None = None
    # Аргументы, которые не пишутся в журнал (вводимый текст и т.п.).
    private_params: tuple[str, ...] = ()
    # Группа для интерфейса и диагностики: system, browser, web, memory...
    category: str = "general"

    def schema(self) -> dict[str, Any]:
        props = {}
        for pname, p in self.params.items():
            prop: dict[str, Any] = {"type": p.type, "description": p.description}
            if p.enum:
                prop["enum"] = p.enum
            props[pname] = prop
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": [n for n, p in self.params.items() if p.required],
                },
            },
        }


class ToolError(ValueError):
    pass


class ToolResult(str):
    """Текст результата (для модели и голоса) плюс структурированные данные для интерфейса.

    ``data`` — словарь с ключом ``kind`` (``web``, ``youtube``, ``page``, ``screenshot``…).
    ``ok=False`` — инструмент не справился; текст объясняет почему.
    """

    data: dict[str, Any] | None
    ok: bool

    def __new__(cls, text: str, data: dict[str, Any] | None = None, ok: bool = True) -> "ToolResult":
        obj = super().__new__(cls, text)
        obj.data = data
        obj.ok = ok
        return obj


class Choice(ToolResult):
    """Результат «нашлось несколько вариантов»: текст вопроса и варианты.

    Это обычная строка (её видит LLM и слышит пользователь), но помощник
    запоминает варианты и следующей репликой («второй», «Telegram») выбирает один.
    """

    options: list[tuple[str, Callable[[], str]]]

    def __new__(cls, text: str, options: list[tuple[str, Callable[[], str]]],
                data: dict[str, Any] | None = None) -> "Choice":
        obj = super().__new__(cls, text, data)
        obj.options = list(options)
        return obj


_PY_TYPES = {"string": str, "integer": int, "boolean": bool}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Инструмент {tool.name} уже зарегистрирован")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise ToolError(f"Неизвестное действие: {name}")
        return self._tools[name]

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

    def tools(self) -> list[Tool]:
        return list(self._tools.values())

    def describe(self) -> list[dict[str, Any]]:
        """Список инструментов для интерфейса и диагностики."""
        return [{"name": t.name, "description": t.description, "category": t.category,
                 "permission": "confirm" if t.dangerous else
                 ("conditional" if t.confirm_check else "auto"),
                 "params": {n: {"type": p.type, "required": p.required, "description": p.description,
                                **({"enum": p.enum} if p.enum else {})}
                            for n, p in t.params.items()}}
                for t in self._tools.values()]

    def needs_confirmation(self, tool: Tool, args: dict[str, Any]) -> str | None:
        """Текст подтверждения или None, если можно выполнять сразу."""
        if tool.dangerous:
            return self.confirm_text(tool, args)
        if tool.confirm_check is not None:
            return tool.confirm_check(args)
        return None

    @staticmethod
    def loggable(tool: Tool, args: dict[str, Any]) -> dict[str, Any]:
        """Аргументы для журнала: приватные заменены длиной."""
        return {k: (f"<{len(str(v))} симв.>" if k in tool.private_params else v)
                for k, v in args.items()}

    def validate(self, name: str, args: Any) -> tuple[Tool, dict[str, Any]]:
        tool = self.get(name)
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ToolError("Аргументы должны быть объектом")
        unknown = set(args) - set(tool.params)
        if unknown:
            raise ToolError(f"Лишние аргументы: {', '.join(sorted(unknown))}")
        clean: dict[str, Any] = {}
        for pname, p in tool.params.items():
            if pname not in args or args[pname] in (None, ""):
                if p.required:
                    raise ToolError(f"Не указан аргумент {pname}")
                continue
            value = args[pname]
            expected = _PY_TYPES[p.type]
            if p.type == "integer" and isinstance(value, str) and value.strip().lstrip("-").isdigit():
                value = int(value)
            if not isinstance(value, expected) or (p.type == "integer" and isinstance(value, bool)):
                raise ToolError(f"Аргумент {pname} должен быть типа {p.type}")
            if isinstance(value, str):
                value = value.strip()
                if len(value) > p.max_length:
                    raise ToolError(f"Аргумент {pname} слишком длинный")
                if any(ord(ch) < 32 for ch in value):
                    raise ToolError(f"Аргумент {pname} содержит управляющие символы")
            if p.enum and value not in p.enum:
                raise ToolError(f"Недопустимое значение {pname}: {value}")
            clean[pname] = value
        return tool, clean

    def confirm_text(self, tool: Tool, args: dict[str, Any]) -> str:
        template = tool.confirm_template or f"выполнить действие {tool.name}"
        try:
            return template.format(**args)
        except (KeyError, IndexError):
            return template
