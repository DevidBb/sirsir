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
    # Не трогать настоящее меню «Пуск» (на Windows CI оно есть).
    cfg.set("windows.launcher.start_menu_dirs", [])
    return cfg


class FakeLLM:
    """Возвращает заранее заданные сообщения и запоминает запросы."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def chat(self, messages, tools=None, on_text=None):
        self.requests.append({"messages": [dict(m) for m in messages], "tools": tools,
                              "stream": on_text is not None})
        if not self.responses:
            r = {"role": "assistant", "content": "ок"}
        else:
            r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        r = dict(r)
        if on_text is not None and r.get("content"):
            # Имитация потока: кусочки по 5 символов, остановка по False.
            text, sent = r["content"], []
            for i in range(0, len(text), 5):
                sent.append(text[i:i + 5])
                if on_text(text[i:i + 5]) is False:
                    break
            r["content"] = "".join(sent)
        return r


def tool_call(tool_name, **args):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"function": {"name": tool_name, "arguments": args}}]}


def text(content):
    return {"role": "assistant", "content": content}
