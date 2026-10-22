"""Память в Obsidian (vault.py): один владелец у каждой записи, без повторов и противоречий,
ссылки для Obsidian и GitHub, индекс для поиска, контекст в промпт и событие для HUD."""

import re
import time
import urllib.parse
from pathlib import Path

import pytest

from conftest import FakeLLM, text, tool_call
from jarvis.assistant import Assistant
from jarvis.memory import Memory
from jarvis.tool_registry import ToolRegistry
from jarvis.vault import (INDEX, INDEX_MARK, MIRROR_DIR, MIRROR_FACTS, Vault, dump_frontmatter, setup,
                          split_frontmatter)

LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")


@pytest.fixture
def mem():
    m = Memory(":memory:")
    yield m
    m.close()


@pytest.fixture
def vault(tmp_path, mem):
    v = Vault(tmp_path / "obsidian", mem, clock=lambda: 1792627200.0)  # 2026-10-22
    v.sync()
    return v


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def broken_links(root: Path) -> list[str]:
    """Каждая относительная markdown-ссылка ведёт на существующий файл (как проверяет vault_lint)."""
    bad = []
    for md in root.rglob("*.md"):
        for target in LINK_RE.findall(read(md)):
            if re.match(r"^[a-z]+:", target) or target.startswith("#"):
                continue
            path = (md.parent / urllib.parse.unquote(target.split("#")[0])).resolve()
            if not path.exists():
                bad.append(f"{md.relative_to(root)} → {target}")
    return bad


def test_decision_is_saved_with_frontmatter_links_and_index(vault):
    r = vault.save("decision", "Сфера HUD на WebGL", "Пишем свою сферу: лицензия эталона некоммерческая.",
                   project="JARVIS HUD", topic="сфера", source="решение владельца")
    assert r["ok"] and r["created"]
    path = vault.root / r["note"]["path"]
    meta, body, had = split_frontmatter(read(path))
    assert had and meta["type"] == "decision" and meta["status"] == "fact" and meta["date"] == "2026-10-22"
    assert meta["project"] == "проект-jarvis-hud" and meta["source"] == "решение владельца"
    assert body.startswith("# Сфера HUD на WebGL") and body.rstrip().splitlines()[-1].startswith("Related: ")
    assert "\r" not in path.read_bytes().decode("utf-8")  # LF и на Windows
    assert (vault.root / "Проекты" / "проект-jarvis-hud.md").exists()  # проект создан для связи
    root_index = read(vault.root / INDEX)
    assert INDEX_MARK in root_index and "Сфера HUD на WebGL" in root_index and "JARVIS HUD" in root_index
    assert "[[" not in root_index  # без wikilinks: GitHub их не показывает
    assert broken_links(vault.root) == []


def test_repeat_is_not_a_new_record(vault):
    first = vault.save("decision", "Сфера на WebGL", "Свой код.", topic="сфера")
    again = vault.save("decision", "Сфера на WebGL", "Свой код.", topic="сфера")
    assert again["ok"] and not again["created"] and again["note"]["path"] == first["note"]["path"]
    assert len(list((vault.root / "Решения").glob("решение-*.md"))) == 1
    task = vault.save("task", "Проверить HUD на Windows", "Открыть run_web.bat")
    assert not vault.save("task", "проверить HUD на windows", "Открыть run_web.bat")["created"]
    assert task["note"]["status"] == "plan"


def test_new_decision_on_same_topic_supersedes_the_old_one(vault, mem):
    old = vault.save("decision", "Сфера на WebGL", "Пишем на чистом WebGL.", topic="сфера")
    new = vault.save("decision", "Сфера на Three.js", "Берём Three.js локально.", topic="сфера")
    assert new["superseded"] == [old["note"]["path"]]
    meta, body, _ = split_frontmatter(read(vault.root / old["note"]["path"]))
    assert meta["status"] == "superseded" and meta["superseded_by"] == Path(new["note"]["path"]).stem
    assert "Заменено решением" in body
    new_meta, _, _ = split_frontmatter(read(vault.root / new["note"]["path"]))
    assert new_meta["supersedes"] == [Path(old["note"]["path"]).stem]
    # одно действующее решение на тему: в промпт и в «последние решения» идёт только новое
    assert [d["title"] for d in vault.recent("decision")] == ["Сфера на Three.js"]
    ctx = mem.context_for("какую сферу пишем")
    assert "Three.js" in ctx and "чистом WebGL" not in ctx
    assert broken_links(vault.root) == []


def test_plan_becomes_done_in_the_same_note(vault):
    vault.save("task", "Проверить HUD на Windows", "Открыть run_web.bat и посмотреть сферу.")
    r = vault.save("task", "Проверить HUD на Windows", "Сфера видна, голос не задерживается.", status="done",
                   source="проверка на ПК")
    files = list((vault.root / "Задачи").glob("задача-*.md"))
    assert len(files) == 1 and r["note"]["status"] == "done"
    text_ = read(files[0])
    assert "## 2026-10-22 — сделано (было: план)" in text_ and "Источник: проверка на ПК" in text_
    assert text_.index("Сфера видна") < text_.index("Related:")
    assert vault.next_steps() == []  # сделанное — не следующий шаг


def test_secrets_are_not_written(vault):
    r = vault.save("check", "Вход в почту", "пароль от почты qwerty123")
    assert not r["ok"] and "пароль" in r["text"]
    assert not list((vault.root / "Проверки").glob("проверка-*.md"))
    assert not vault.save("task", "x", "", status="unknown")["ok"]
    assert not vault.save("idea", "x")["ok"]


def test_notes_written_in_obsidian_are_indexed_and_found(vault, mem):
    own = vault.root / "Мои заметки" / "идеи для канала.md"
    own.parent.mkdir()
    own.write_text("---\ntype: решение\nstatus: предположение\n---\n# Шортсы про ИИ\n\n"
                   "Снимать короткие ролики про локальных ассистентов.\napi_key = sk-abcdefghijklmnopqrstuv\n",
                   encoding="utf-8")
    vault._scanned = 0
    vault.refresh()
    found = mem.search("ролики про ассистентов")
    assert found and found[0]["kind"] == "vault" and found[0]["path"] == "Мои заметки/идеи для канала.md"
    assert found[0]["type"] == "decision" and found[0]["status"] == "assumption"  # русские значения поняты
    assert mem.search("sk-abcdefghijklmnopqrstuv") == []  # строки с ключами не индексируются
    assert "[решение · предположение]" in mem.context_for("ролики про ассистентов")
    own.unlink()
    vault._scanned = 0
    vault.refresh()
    assert mem.search("ролики про ассистентов") == []  # удалённый файл ушёл из индекса


def test_mirror_is_read_only_and_hand_edits_are_kept(vault, mem):
    mem.add_fact("Меня зовут Дэвид")
    mem.add_fact("Отвечай коротко", "preference")
    vault.sync()
    mirror = vault.root / MIRROR_DIR / MIRROR_FACTS
    assert "Меня зовут Дэвид" in read(mirror) and "Отвечай коротко" in read(mirror)
    assert mem.search("Дэвид")[0]["kind"] == "fact"  # зеркало не попадает в индекс заметок второй раз
    assert all(n["path"].split("/")[0] != MIRROR_DIR for n in mem.vault_notes())
    mirror.write_text(read(mirror) + "\n- Я живу в Таллине\n", encoding="utf-8")  # правка руками
    mem.add_fact("Люблю чай")
    vault.sync()
    edits = vault.edits()
    assert len(edits) == 1 and "Я живу в Таллине" in read(vault.root / edits[0])
    assert "Я живу в Таллине" not in read(mirror) and "Люблю чай" in read(mirror)
    assert "Нужно разобрать" in read(vault.root / INDEX)
    assert broken_links(vault.root) == []


def test_own_index_is_not_overwritten(tmp_path, mem):
    root = tmp_path / "vault"
    root.mkdir()
    (root / INDEX).write_text("# Мой указатель\n", encoding="utf-8")
    v = Vault(root, mem)
    v.sync()
    assert read(root / INDEX) == "# Мой указатель\n"
    assert (root / "Решения" / INDEX).exists()


def test_frontmatter_round_trip():
    meta = {"title": "Решение: да, но позже", "type": "decision", "date": "2026-10-22", "tags": ["jarvis", "a b"],
            "source": "https://example.com/x?y=1", "flag": "true", "n": "12"}
    text_ = dump_frontmatter(meta) + "\n# Т\n"
    back, body, had = split_frontmatter(text_)
    assert had and back == meta and body == "# Т\n"
    back, _, _ = split_frontmatter("---\ntags:\n  - a\n  - b\ntitle: 'It''s'\n---\nx")
    assert back == {"tags": ["a", "b"], "title": "It's"}
    assert split_frontmatter("no frontmatter")[2] is False


def test_setup_paths(config, tmp_path):
    v = setup(config, Memory(":memory:"))
    assert v.root == Path(config.path("memory.db_path")).parent / "obsidian"
    config.set("memory.vault.path", str(tmp_path / "Obsidian" / "Джарвис"))
    assert setup(config, Memory(":memory:")).root == tmp_path / "Obsidian" / "Джарвис"
    config.set("memory.vault.enabled", False)
    assert setup(config, Memory(":memory:")) is None


# ---------- ядро: контекст перед задачей, событие для HUD, сохранение итогов ----------
@pytest.fixture
def assistant(config, tmp_path):
    registry = ToolRegistry()
    m = Memory(":memory:")
    m.register_tools(registry)
    v = Vault(tmp_path / "vault", m)
    v.register_tools(registry)
    a = Assistant(config, FakeLLM([]), registry, m)
    a.vault = v
    events = []
    a.on_event = events.append
    a.events = events
    return a


def test_context_before_task_is_shown_exactly_as_sent(assistant):
    assistant.vault.save("decision", "Голос — Silero", "Silero звучит лучше системного голоса.", topic="голос")
    assistant.memory.add_fact("Отвечай коротко", "preference")
    assistant.llm.responses = [text("Голос Silero.")]
    assistant.handle("какой голос мы выбрали и почему он лучше")
    ev = [e for e in assistant.events if e["type"] == "memory"][-1]
    assert ev["used_memory"] and ev["query"] == "какой голос мы выбрали и почему он лучше"
    labels = [u["label"] for u in ev["used"]]
    assert "предпочтение" in labels and any(lb.startswith("решение · факт") for lb in labels)
    assert ev["related"][0]["title"] == "Голос — Silero" and ev["related"][0]["uri"].startswith("obsidian://open?path=")
    system = assistant.llm.requests[0]["messages"][0]["content"]
    assert all(u["text"] in system for u in ev["used"])  # в HUD — ровно то, что получила модель
    assert "save_memory_note" in system  # модель знает, когда сохранять итоги


def test_model_saves_decision_with_tool(assistant):
    assistant.llm.responses = [
        tool_call("save_memory_note", type="decision", title="Хранить память в Obsidian",
                  text="Факты в SQLite, проекты и решения — файлами.", status="fact", topic="память"),
        text("Записал решение.")]
    reply = assistant.handle("решили: память проектов храним в Obsidian, запиши это")
    assert reply.text == "Записал решение."
    assert [d["title"] for d in assistant.vault.recent("decision")] == ["Хранить память в Obsidian"]


def test_voice_phrase_saves_without_model(assistant):
    reply = assistant.handle("Запиши решение: сферу пишем на WebGL, без Three.js")
    assert assistant.llm.requests == [] and "Записал решение" in reply.text
    reply = assistant.handle("следующий шаг — проверить HUD на Windows")
    assert "задача «Проверить HUD на Windows» (план)" in reply.text
    assert [s["title"] for s in assistant.vault.next_steps()] == ["Проверить HUD на Windows"]


def test_team_mode_reports_memory_is_not_used(assistant):
    class Team:
        def run(self, *a, **k):
            raise AssertionError("не должен вызываться в тесте")
    assistant.config.set("llm.provider", "team")
    assistant.team = Team()
    assistant._team_command = lambda *a, **k: text("ок")  # путь команды без настоящих ИИ
    assistant.memory_turn("вопрос", used=False)
    ev = assistant.events[-1]
    assert ev["type"] == "memory" and ev["used"] == [] and ev["used_memory"] is False and "Команда" in ev["note"]


def test_fact_duplicates_with_other_case_are_skipped():
    m = Memory(":memory:")
    assert m.add_fact("Меня зовут Давид")
    assert not m.add_fact("меня зовут Давид.")
    assert len(m.list_facts()) == 1


def test_update_keeps_properties_added_in_obsidian(vault):
    r = vault.save("task", "Проверить HUD на Windows", "Открыть run_web.bat.")
    path = vault.root / r["note"]["path"]
    path.write_text(read(path).replace("status: plan\n", "status: plan\nurgent: true\npriority: 2\n"
                                       "description: |\n  Сфера и панель памяти.\n  На ПК с RTX 3060.\n"
                                       "# своё поле ниже\ndeadline: 2026-11-01T18:00\n"), encoding="utf-8")
    vault.save("task", "Проверить HUD на Windows", "Готово.", status="done")
    after = read(path)
    for line in ("urgent: true\n", "priority: 2\n", "description: |\n  Сфера и панель памяти.\n  На ПК с RTX 3060.\n",
                 "# своё поле ниже\n", "deadline: 2026-11-01T18:00\n", "status: done\n"):
        assert line in after
    meta, _, _ = split_frontmatter(after)
    assert meta["description"] == "Сфера и панель памяти.\nНа ПК с RTX 3060." and meta["status"] == "done"


def test_superseding_keeps_properties_added_in_obsidian(vault):
    old = vault.save("decision", "Сфера на WebGL", "Пишем на чистом WebGL.", topic="сфера")
    path = vault.root / old["note"]["path"]
    path.write_text(read(path).replace("status: fact\n", "status: fact\nreviewed: true\n"), encoding="utf-8")
    vault.save("decision", "Сфера на Three.js", "Берём Three.js.", topic="сфера")
    after = read(path)
    assert "reviewed: true\n" in after and "status: superseded\n" in after


def test_secrets_in_properties_and_headings_stay_out_of_index_and_prompt(vault, mem):
    (vault.root / "Проверки" / "роутер.md").write_text(
        "---\ntitle: Роутер\ndescription: пароль от роутера Qwerty2024!\n---\n\nРоутер настроен.\n", encoding="utf-8")
    (vault.root / "Мои").mkdir()
    (vault.root / "Мои" / "почта.md").write_text("# Пароль от почты: Zx9vB7nM\n\nПочтовый ящик настроен.\n",
                                                 encoding="utf-8")
    vault.sync()
    ctx = mem.context_for("роутер почта настроен")
    indexes = "".join(read(p) for p in vault.root.rglob(INDEX))
    assert "Роутер" in ctx
    for secret in ("Qwerty2024", "Zx9vB7nM"):
        assert secret not in ctx and secret not in indexes and not mem.search(secret)


def test_different_records_with_one_file_name_stay_apart(vault):
    vault.save("task", "Переписать модуль на C++")
    r = vault.save("task", "Переписать модуль на C#")
    assert r["created"] and r["text"].startswith("Записал")
    titles = sorted(vault._file_title(p) for p in (vault.root / "Задачи").glob("задача-*.md"))
    assert titles == ["Переписать модуль на C#", "Переписать модуль на C++"]
    assert vault.save("task", "???")["created"] and vault.save("task", "!!!")["created"]
    long = "Обновить драйвер видеокарты на рабочем компьютере и проверить сферу на "
    vault.save("decision", long + "NVIDIA")
    b = vault.save("decision", long + "AMD")
    assert b["superseded"] == [] and {d["title"] for d in vault.recent("decision")} == {long + "NVIDIA", long + "AMD"}
    assert broken_links(vault.root) == []


def test_edit_in_obsidian_is_seen_by_the_very_next_task(vault, mem):
    r = vault.save("decision", "Голос", "Используем pyttsx3.")
    path = vault.root / r["note"]["path"]
    path.write_text(read(path).replace("pyttsx3", "Silero"), encoding="utf-8")
    vault.refresh()  # сразу, без паузы
    assert "Silero" in mem.context_for("какой голос")


def test_sync_reparses_only_changed_files_and_rebuilds_after_a_format_change(vault, mem, monkeypatch):
    for i in range(5):
        vault.save("task", f"Шаг номер {i}")
    calls = []
    real = vault._parse
    monkeypatch.setattr(vault, "_parse", lambda *a: calls.append(a[1]) or real(*a))
    vault.sync()
    assert calls == []
    mem.set_setting("vault.index_version", 1)  # разбор изменился в новой версии
    vault.sync()
    assert len(calls) == 5 and len(mem.vault_notes()) == 5 and mem.search("Шаг номер")
    assert mem.get_setting("vault.index_version") != 1


def test_close_cancels_pending_sync(vault, mem, caplog):
    vault.sync_later(delay=0.05)
    vault.close()
    mem.close()
    vault.sync_later(delay=0.01)  # после закрытия — ничего
    time.sleep(0.2)
    assert "closed database" not in caplog.text


def test_voice_decision_with_the_same_title_supersedes_the_old_one(vault):
    from jarvis.intents import IntentMatcher
    matcher = IntentMatcher({}, {})
    first = matcher.match("запиши решение: сфера — пишем на WebGL").args
    second = matcher.match("запиши решение: сфера — пишем на Three.js").args
    old = vault.save(first["type"], first["title"], first["text"], first["status"])
    new = vault.save(second["type"], second["title"], second["text"], second["status"])
    assert new["superseded"] == [old["note"]["path"]]
    assert [d["path"] for d in vault.recent("decision")] == [new["note"]["path"]]
