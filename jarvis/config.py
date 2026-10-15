"""Загрузка конфигурации из config.toml (только стандартная библиотека)."""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"


class Config:
    """Обёртка над словарём настроек с доступом через get("section.key")."""

    def __init__(self, data: dict[str, Any], root: Path = PROJECT_ROOT):
        self.data = data
        self.root = root

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        path = Path(path) if path else DEFAULT_CONFIG_PATH
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return cls(data, root=path.resolve().parent)

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
