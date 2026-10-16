"""«Посмотри на экран»: снимок только по просьбе пользователя, честно о модели без зрения."""

import pytest

pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from conftest import FakeLLM  # noqa: E402
from jarvis.assistant import Assistant  # noqa: E402
from jarvis.intents import IntentMatcher  # noqa: E402
from jarvis.memory import Memory  # noqa: E402
from jarvis.screen import ScreenError, capture_screen, image_for_model  # noqa: E402
from jarvis.tool_registry import ToolRegistry  # noqa: E402


def fake_grab():
    return Image.new("RGB", (3200, 1800), (10, 20, 30))


class VisionLLM(FakeLLM):
    def chat(self, messages, tools=None, on_text=None, options=None, model=None):
        self.requests.append({"messages": messages, "model": model})
        return {"role": "assistant", "content": "Вижу тёмный экран."}


@pytest.fixture
def assistant(config, tmp_path):
    a = Assistant(config, FakeLLM([]), ToolRegistry(), Memory(":memory:"))
    a.screen_dir = tmp_path / "shots"
    a.screen_grabber = fake_grab
    return a


@pytest.mark.parametrize("phrase", ["Посмотри на экран и объясни, что происходит", "что у меня на экране?",
                                    "глянь на экран, что за ошибка", "look at my screen"])
def test_look_intents(phrase):
    assert IntentMatcher({}, {}).match(phrase).tool == "look_at_screen"


def test_screenshot_intent():
    assert IntentMatcher({}, {}).match("сделай скриншот экрана").tool == "screen_screenshot"


def test_image_for_model_downscales(tmp_path):
    path = capture_screen(tmp_path, fake_grab)
    data, mime = image_for_model(path)
    import base64, io
    im = Image.open(io.BytesIO(base64.b64decode(data)))
    assert mime == "image/jpeg" and max(im.size) == 1568


def test_without_vision_model_is_honest(assistant, config):
    config.set("llm.vision_model", "")
    reply = assistant.handle("посмотри на экран")
    assert "не понимает изображения" in reply.text and reply.data["kind"] == "screen"
    assert list(assistant.screen_dir.glob("screen-*.png"))


def test_ollama_vision_model(assistant, config):
    config.set("llm.vision_model", "qwen2.5vl:7b")
    assistant.llm = VisionLLM([])
    reply = assistant.handle("что у меня на экране")
    assert reply.text == "Вижу тёмный экран."
    req = assistant.llm.requests[0]
    assert req["model"] == "qwen2.5vl:7b" and req["messages"][0]["images"]


def test_agent_backend_gets_image(assistant):
    from jarvis.assistant import Reply
    seen = {}

    class Backend:
        def run(self, a, text, on_sentence, image=None):
            seen["image"], seen["text"] = image, text
            return Reply("Это редактор кода.")
    assistant.agent_backend = Backend()
    reply = assistant.handle("посмотри на экран и скажи, что открыто")
    assert reply.text == "Это редактор кода." and seen["image"][1] == "image/jpeg"
    assert "недоверенные" in seen["text"] and reply.data["kind"] == "screen"


def test_capture_failure_is_reported(assistant):
    def broken():
        raise ScreenError("нет доступа к экрану")
    assistant.screen_grabber = broken
    reply = assistant.handle("посмотри на экран")
    assert "Не удалось посмотреть на экран: нет доступа к экрану" == reply.text


def test_model_screen_tool_never_returns_image(config):
    from jarvis.main import App
    config.set("browser.enabled", False)
    app = App(config, voice=False)
    tool = app.registry.get("screen_screenshot")
    assert "не передаётся" in tool.description
    app.close()
