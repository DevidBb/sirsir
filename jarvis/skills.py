"""Навыки (Skills): повторяемые сценарии — инструкция плюс набор инструментов.

Формат совместим со Skills Claude: папка ``skills/<имя>/SKILL.md`` с YAML-заголовком
``name`` и ``description`` и инструкцией в тексте. Дополнения Джарвиса (необязательны):

* ``triggers`` — фразы, по которым навык включается сам («сравни источники»);
* ``tools`` — какие инструменты доступны модели в навыке (маски вида ``files_*``);
  пусто — все инструменты Джарвиса;
* ``max_steps`` — сколько вызовов инструментов можно за одну задачу (для Ollama, до 12).

Навык не добавляет возможностей: инструменты те же, с теми же проверками и
подтверждениями; скрипты из папки навыка не запускаются. Список навыков модель видит
в промпте и может загрузить подходящий инструментом ``use_skill``.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .security import normalize
from .tool_registry import Param, Tool, ToolRegistry, ToolResult

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_INSTRUCTIONS = 12_000
MAX_STEPS = 12
EXPLICIT_RE = re.compile(r"^(?:используй |запусти |включи )?(?:навык|сценарий|skill)\s+([\w-]+)[\s,:.—-]*(.*)$",
                         re.I | re.S)


@dataclass
class Skill:
    name: str
    description: str
    instructions: str
    triggers: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    max_steps: int | None = None
    path: str = ""

    def allows(self, tool_name: str) -> bool:
        return not self.tools or any(fnmatch.fnmatchcase(tool_name, p) for p in self.tools)

    def prompt(self) -> str:
        return (f"\nСейчас действует навык «{self.name}» ({self.description}). Следуй его инструкции; "
                "правила безопасности и подтверждения она не отменяет.\n"
                f"<skill name=\"{self.name}\">\n{self.instructions}\n</skill>\n")


def parse_skill(text: str, path: str = "", folder_name: str = "") -> Skill:
    """SKILL.md → Skill. Ошибка формата — ValueError с понятным текстом."""
    import yaml
    match = re.match(r"^﻿?---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not match:
        raise ValueError("нет заголовка между строками ---")
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"ошибка YAML: {e}") from e
    if not isinstance(meta, dict):
        raise ValueError("заголовок должен быть набором «ключ: значение»")
    name = str(meta.get("name") or folder_name).strip()
    if not NAME_RE.match(name):
        raise ValueError(f"имя «{name}»: строчные латинские буквы, цифры и дефис")
    description = " ".join(str(meta.get("description") or "").split())
    if not description:
        raise ValueError("нет description")
    instructions = match.group(2).strip()
    if not instructions:
        raise ValueError("нет текста инструкции")
    if len(instructions) > MAX_INSTRUCTIONS:
        raise ValueError(f"инструкция длиннее {MAX_INSTRUCTIONS} символов")

    def strings(key: str) -> list[str]:
        value = meta.get(key) or []
        if isinstance(value, str):
            value = [v.strip() for v in value.split(",")]
        if not isinstance(value, list):
            raise ValueError(f"{key} — список")
        return [str(v).strip() for v in value if str(v).strip()]
    steps = meta.get("max_steps")
    if steps is not None:
        if not isinstance(steps, int) or isinstance(steps, bool) or steps < 1:
            raise ValueError("max_steps — целое число от 1")
        steps = min(steps, MAX_STEPS)
    return Skill(name, description[:300], instructions, strings("triggers"), strings("tools"), steps, path)


class SkillLibrary:
    def __init__(self, folders: list[str | Path]) -> None:
        self.folders = [Path(f) for f in folders]
        self.skills: dict[str, Skill] = {}
        self.errors: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        skills, errors = {}, {}
        for folder in self.folders:
            if not folder.is_dir():
                continue
            for file in sorted(folder.glob("*/SKILL.md")):
                key = file.parent.name
                try:
                    skill = parse_skill(file.read_text(encoding="utf-8"), str(file), key)
                except (OSError, UnicodeDecodeError, ValueError) as e:
                    errors[key] = str(e)
                    log.warning("Навык %s не загружен: %s", file, e)
                    continue
                if skill.name in skills:
                    errors[key] = f"имя {skill.name} уже занято"
                    continue
                skills[skill.name] = skill
        self.skills, self.errors = skills, errors

    def get(self, name: str) -> Skill | None:
        return self.skills.get(name.strip().lower())

    def match(self, text: str) -> tuple[Skill, str] | None:
        """Навык для реплики и текст задачи: «навык X …» или фраза из triggers."""
        explicit = EXPLICIT_RE.match(text.strip())
        if explicit and self.get(explicit.group(1)):
            return self.get(explicit.group(1)), explicit.group(2).strip() or text
        said = f" {normalize(text)} "
        best = None
        for skill in self.skills.values():
            for trigger in skill.triggers:
                phrase = normalize(trigger)
                if phrase and f" {phrase} " in said and (best is None or len(phrase) > best[1]):
                    best = (skill, len(phrase))
        return (best[0], text) if best else None

    def catalog(self) -> str:
        if not self.skills:
            return ""
        lines = "\n".join(f"- {s.name}: {s.description}" for s in self.skills.values())
        return ("\nНавыки (готовые сценарии). Если задача подходит под навык, сначала вызови "
                f"use_skill с его именем и следуй полученной инструкции:\n{lines}\n")

    def describe(self, registry: ToolRegistry | None = None) -> list[dict[str, Any]]:
        names = registry.names() if registry else []
        out = []
        for s in self.skills.values():
            missing = [p for p in s.tools if names and not any(fnmatch.fnmatchcase(n, p) for n in names)]
            out.append({"name": s.name, "description": s.description, "triggers": s.triggers,
                        "tools": s.tools, "missing_tools": missing, "max_steps": s.max_steps,
                        "path": s.path, "error": ""})
        out += [{"name": k, "description": "", "triggers": [], "tools": [], "missing_tools": [],
                 "max_steps": None, "path": "", "error": e} for k, e in self.errors.items()]
        return out

    def register_tools(self, registry: ToolRegistry) -> None:
        def use_skill(name: str) -> ToolResult:
            skill = self.get(name)
            if skill is None:
                known = ", ".join(self.skills) or "нет"
                return ToolResult(f"Навыка {name} нет. Есть: {known}.", ok=False)
            tools = ", ".join(skill.tools) if skill.tools else "любые инструменты Джарвиса"
            return ToolResult(f"Инструкция навыка «{skill.name}» (инструменты: {tools}). Правила "
                              f"безопасности и подтверждения она не отменяет.\n<skill name=\"{skill.name}\">\n"
                              f"{skill.instructions}\n</skill>", {"kind": "skill", "name": skill.name})
        registry.register(Tool("use_skill", "Загрузить инструкцию навыка (сценария) по имени из списка навыков",
                               use_skill, {"name": Param("string", "Имя навыка", max_length=64)},
                               category="skills"))


def setup(config, registry: ToolRegistry) -> SkillLibrary | None:
    if not config.get("skills.enabled", True):
        return None
    folders = [config.root / "skills"] + [Path(p).expanduser() for p in config.get("skills.extra_dirs", []) or []]
    library = SkillLibrary(folders)
    library.register_tools(registry)
    return library
