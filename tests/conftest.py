import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.config import Config  # noqa: E402


@pytest.fixture
def config(tmp_path):
    cfg = Config.load(ROOT / "config.toml")
    allowed = tmp_path / "docs"
    allowed.mkdir()
    cfg.set("windows.search.allowed_dirs", [str(allowed)])
    cfg.set("memory.db_path", str(tmp_path / "memory.db"))
    return cfg


class FakeLLM:
    """Возвращает заранее заданные сообщения и запоминает запросы."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def chat(self, messages, tools=None):
        self.requests.append({"messages": [dict(m) for m in messages], "tools": tools})
        if not self.responses:
            return {"role": "assistant", "content": "ок"}
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return dict(r)


def tool_call(tool_name, **args):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"function": {"name": tool_name, "arguments": args}}]}


def text(content):
    return {"role": "assistant", "content": content}
