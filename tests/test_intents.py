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


@pytest.mark.parametrize("phrase,intent", [
    ("Поставь таймер на 5 минут", Intent("set_timer", {"seconds": 300})),
    ("таймер на полчаса", Intent("set_timer", {"seconds": 1800})),
    ("Напомни через 10 минут позвонить маме",
     Intent("set_reminder", {"text": "позвонить маме", "minutes": 10})),
    ("напомни мне через двадцать пять минут, что надо выключить плиту",
     Intent("set_reminder", {"text": "надо выключить плиту", "minutes": 25})),
    ("Напомни в 18:30 забрать посылку",
     Intent("set_reminder", {"text": "забрать посылку", "at_time": "18:30"})),
    ("какие напоминания", Intent("list_reminders")),
    ("отмени таймер", Intent("cancel_reminders")),
])
def test_reminder_intents(m, phrase, intent):
    assert m.match(phrase) == intent


def test_timer_without_duration_goes_to_llm(m):
    assert m.match("поставь таймер на варку яиц") is None
