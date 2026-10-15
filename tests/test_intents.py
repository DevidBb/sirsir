import pytest

from jarvis.intents import Intent, IntentMatcher

APPS = {"блокнот": "notepad.exe", "диспетчер задач": "taskmgr.exe"}
SITES = {"ютуб": "https://www.youtube.com"}


@pytest.fixture
def m():
    return IntentMatcher(APPS, SITES)


@pytest.mark.parametrize("phrase,intent", [
    ("Открой блокнот", Intent("open_app", {"name": "блокнот"})),
    ("запусти диспетчер задач", Intent("open_app", {"name": "диспетчер задач"})),
    ("пожалуйста, открой мне приложение блокнот", Intent("open_app", {"name": "блокнот"})),
    ("открой сайт ютуб", Intent("open_website", {"name": "ютуб"})),
    ("Который час?", Intent("current_time")),
    ("сколько свободно памяти", Intent("system_info")),
    ("сделай погромче", Intent("media_control", {"action": "volume_up"})),
    ("тише", Intent("media_control", {"action": "volume_down"})),
    ("следующий трек", Intent("media_control", {"action": "next"})),
    ("поставь на паузу", Intent("media_control", {"action": "play_pause"})),
])
def test_matches(m, phrase, intent):
    assert m.match(phrase) == intent


@pytest.mark.parametrize("phrase", [
    "открой фотошоп",                 # не в белом списке → LLM
    "что такое чёрная дыра",
    "открой блокнот и напиши в нём длинное письмо маме про выходные",  # длинная фраза → LLM
    "",
])
def test_no_match(m, phrase):
    assert m.match(phrase) is None
