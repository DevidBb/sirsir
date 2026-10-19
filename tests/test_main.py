from jarvis import checks
from jarvis.main import App


def test_config_loads(config):
    assert config.get("llm.model") == "qwen3:8b"
    assert "блокнот" in config.get("windows.apps")
    assert config.get("nope.nothing", 5) == 5


def test_app_text_mode_settings_persist(config, monkeypatch):
    app = App(config, voice=False)
    app.assistant.registry.get("change_setting").handler(key="tts.rate", value="150")
    assert app.speaker.rate == 150
    app.close()
    app2 = App(config, voice=False)
    assert app2.config.get("tts.rate") == 150
    app2.close()


def test_app_respond_exit(config, capsys):
    app = App(config, voice=False)
    app.respond("заверши работу")
    assert app.stop_event.is_set()
    assert "До свидания" in capsys.readouterr().out
    app.close()


def test_checks_report_missing_ollama(config, capsys):
    config.set("llm.base_url", "http://127.0.0.1:9")
    results = checks.run_all(config, voice=False)
    assert not checks.report(results)
    assert "Ollama" in capsys.readouterr().out


def test_text_mode_reminder_end_to_end(config, capsys):
    app = App(config, voice=False)
    reply = app.assistant.handle("Поставь таймер на 5 минут")
    assert reply.text == "Таймер на 5 минут запущен."
    app.reminders.clock = lambda: 10**12  # «прошло много времени»
    due = app.reminders.pop_due()
    assert len(due) == 1 and due[0][1] == "timer"
    app.close()


def test_switch_to_gemini_swaps_llm_client(config, monkeypatch):
    from jarvis.gemini import GeminiClient
    from jarvis.llm import OllamaClient
    monkeypatch.setattr(OllamaClient, "unload", lambda self: None)
    app = App(config, voice=False)
    app.set_provider("gemini")
    assert isinstance(app.llm, GeminiClient) and app.assistant.llm is app.llm
    assert app.assistant.agent_backend is None
    app.set_provider("ollama")
    assert isinstance(app.llm, OllamaClient) and app.assistant.llm is app.llm
    app.close()
