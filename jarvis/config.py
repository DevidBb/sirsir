"""Загрузка конфигурации из config.toml (только стандартная библиотека)."""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"
# Личные настройки владельца: не в git, поверх config.toml — обновление не создаёт конфликтов.
LOCAL_CONFIG_NAME = "config.local.toml"


class Config:
    """Обёртка над словарём настроек с доступом через get("section.key")."""

    def __init__(self, data: dict[str, Any], root: Path = PROJECT_ROOT):
        self.data = data
        self.root = root
        self.local_path: Path | None = None  # config.local.toml, если он применён

    @classmethod
    def load(cls, path: str | Path | None = None, local: bool | None = None) -> "Config":
        """config.toml и поверх него config.local.toml из той же папки.

        По умолчанию личный файл применяется только к основному config.toml (path не задан):
        тесты и проверки, передающие путь явно, не зависят от настроек владельца.
        """
        if local is None:
            local = path is None
        path = Path(path) if path else DEFAULT_CONFIG_PATH
        with open(path, "rb") as f:
            data = tomllib.load(f)
        config = cls(data, root=path.resolve().parent)
        local_path = path.resolve().parent / LOCAL_CONFIG_NAME
        if local and local_path.is_file():
            try:
                with open(local_path, "rb") as f:
                    overrides = tomllib.load(f)
            except tomllib.TOMLDecodeError as e:
                raise ValueError(f"ошибка в {LOCAL_CONFIG_NAME}: {e}") from e
            merge(config.data, overrides)
            config.local_path = local_path
        return config

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return copy.deepcopy(node)

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def path(self, dotted: str) -> Path:
        """Путь из конфига; относительные пути — от каталога config.toml."""
        p = Path(str(self.get(dotted))).expanduser()
        return p if p.is_absolute() else self.root / p


def merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Рекурсивно перенести overrides в base: таблицы сливаются, остальное заменяется."""
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def diff(base: dict[str, Any], current: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Что в current отличается от base: (изменённые и новые ключи, имена удалённых ключей)."""
    changed: dict[str, Any] = {}
    removed: list[str] = []
    for key, value in current.items():
        old = base.get(key)
        if isinstance(value, dict) and isinstance(old, dict):
            sub, sub_removed = diff(old, value)
            if sub:
                changed[key] = sub
            removed += [f"{key}.{name}" for name in sub_removed]
        elif key not in base or old != value:
            changed[key] = copy.deepcopy(value)
    removed += [key for key in base if key not in current]
    return changed, removed


def _toml_key(key: str) -> str:
    if key and all(c.isalnum() or c in "_-" for c in key) and key.isascii():
        return key
    return _toml_value(key)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        out = ['"']
        for c in value:
            if c in '"\\':
                out.append("\\" + c)
            elif c == "\n":
                out.append("\\n")
            elif c == "\t":
                out.append("\\t")
            elif ord(c) < 0x20 or ord(c) == 0x7F:
                out.append(f"\\u{ord(c):04x}")
            else:
                out.append(c)
        return "".join(out) + '"'
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in value.items()) + "}"
    raise TypeError(f"значение {type(value).__name__} нельзя записать в TOML")


def dumps_toml(data: dict[str, Any], header: str = "") -> str:
    """Записать словарь настроек в TOML (без массивов таблиц — в config.toml их нет)."""
    lines = [f"# {line}".rstrip() for line in header.splitlines()]

    def table(node: dict[str, Any], prefix: list[str]) -> None:
        plain = [(k, v) for k, v in node.items() if not isinstance(v, dict)]
        tables = [(k, v) for k, v in node.items() if isinstance(v, dict)]
        if prefix and (plain or not tables):
            if lines:
                lines.append("")
            lines.append("[" + ".".join(_toml_key(p) for p in prefix) + "]")
        for key, value in plain:
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
        for key, value in tables:
            table(value, prefix + [key])

    table(data, [])
    return "\n".join(lines) + "\n"


def load_dotenv(path: Path) -> list[str]:
    """Переменные из .env (KEY=VALUE) без сторонних пакетов; окружение имеет приоритет.

    Возвращает только имена загруженных переменных — значения нигде не печатаются.
    """
    import os
    loaded = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return loaded
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded
