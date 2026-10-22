from jarvis.memory import Memory
from jarvis.tool_registry import ToolRegistry


def test_settings_roundtrip(tmp_path):
    m = Memory(tmp_path / "m.db")
    m.set_setting("tts.rate", 200)
    m.set_setting("tts.rate", 210)
    assert m.get_setting("tts.rate") == 210
    assert m.get_setting("missing", "d") == "d"
    m.close()
    assert Memory(tmp_path / "m.db").all_settings() == {"tts.rate": 210}


def test_facts_limit_dedupe_clear(tmp_path):
    m = Memory(tmp_path / "m.db", max_facts=3)
    assert m.add_fact("меня зовут Давид")
    assert not m.add_fact("меня  зовут Давид")
    for i in range(4):
        m.add_fact(f"факт {i}")
    assert m.facts() == ["факт 1", "факт 2", "факт 3"]
    assert m.forget_fact("факт 2") == 1
    assert m.clear_facts() == 2
    assert m.facts() == []


def test_change_setting_tool_validates_range(tmp_path):
    m = Memory(tmp_path / "m.db")
    changed = []
    r = ToolRegistry()
    m.register_tools(r, on_setting_changed=lambda k, v: changed.append((k, v)))
    tool = r.get("change_setting")
    assert tool.dangerous
    assert "от 80" in tool.handler(key="tts.rate", value="9999")
    assert "теперь 200" in tool.handler(key="tts.rate", value="200")
    assert changed == [("tts.rate", 200)]
    assert m.get_setting("tts.rate") == 200


def test_fact_duplicates_ignore_case_spaces_and_end_punctuation_only(tmp_path):
    m = Memory(tmp_path / "m.db")
    assert m.add_fact("Меня зовут Давид")
    assert not m.add_fact("меня зовут давид.")
    assert m.add_fact("Пишу на C++") and m.add_fact("Пишу на C#")  # знаки внутри различают факты
    assert m.add_fact("Сестра — Аня") and m.add_fact("Сестра Аня")
    m.close()
