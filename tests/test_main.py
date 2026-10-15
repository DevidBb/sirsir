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
