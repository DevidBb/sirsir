import pytest

from jarvis.intents import Intent, IntentMatcher

APPS = {"блокнот": "notepad.exe", "диспетчер задач": "taskmgr.exe"}
SITES = {"ютуб": "https://www.youtube.com"}


@pytest.fixture
def m():
    return IntentMatcher(APPS, SITES)


@pytest.mark.parametrize("phrase,intent", [
    ("Открой блокнот", Intent("open_app", {"name": "блокнот"})),
    ("Откроет Ютуб", Intent("open_website", {"name": "ютуб"})),
    ("Открой YouTube.", Intent("open_website", {"name": "ютуб"})),
    ("открой ютюб", Intent("open_website", {"name": "ютуб"})),
    ("открой ютубе", Intent("open_website", {"name": "ютуб"})),
    ("открой ютуб музыку", Intent("open_anything", {"name": "ютуб музыку"})),
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


@pytest.mark.parametrize("phrase,intent", [
    # вежливые слова в любом месте
    ("открой, пожалуйста, блокнот", Intent("open_app", {"name": "блокнот"})),
    ("открой блокнот, пожалуйста", Intent("open_app", {"name": "блокнот"})),
    ("сделай погромче, пожалуйста", Intent("media_control", {"action": "volume_up"})),
    ("зайди на ютуб", Intent("open_website", {"name": "ютуб"})),
    # время и дата разговорными фразами
    ("Сколько сейчас времени?", Intent("current_time")),
    ("который сейчас час", Intent("current_time")),
    ("сколько время", Intent("current_time")),
    ("какой сегодня день недели", Intent("current_time")),
    # громкость и музыка
    ("сделай звук погромче", Intent("media_control", {"action": "volume_up"})),
    ("прибавь громкость", Intent("media_control", {"action": "volume_up"})),
    ("сделай звук тише", Intent("media_control", {"action": "volume_down"})),
    ("громкость меньше", Intent("media_control", {"action": "volume_down"})),
    ("поставь музыку на паузу", Intent("media_control", {"action": "play_pause"})),
    ("включи следующий трек", Intent("media_control", {"action": "next"})),
    ("следующую песню", Intent("media_control", {"action": "next"})),
    ("пропусти трек", Intent("media_control", {"action": "next"})),
    ("верни предыдущую песню", Intent("media_control", {"action": "previous"})),
    # таймеры и будильник
    ("засеки 5 минут", Intent("set_timer", {"seconds": 300})),
    ("засеки пять минут", Intent("set_timer", {"seconds": 300})),
    ("таймер на 1 минуту 30 секунд", Intent("set_timer", {"seconds": 90})),
    ("Поставь будильник на 7:30", Intent("set_reminder", {"text": "Будильник", "at_time": "07:30"})),
    ("разбуди меня в 6 45", Intent("set_reminder", {"text": "Будильник", "at_time": "06:45"})),
])
def test_more_everyday_phrases(m, phrase, intent):
    assert m.match(phrase) == intent


@pytest.mark.parametrize("phrase", [
    "поставь будильник на 25 30",     # нет такого времени
    "включи музыку",                   # play_pause переключает — неоднозначно, решает LLM
    "продолжи",                        # продолжение ответа, а не музыки
    "напомни через 1 минуту 30 секунд проверить чайник",  # секунды не теряем в тексте
])
def test_ambiguous_phrases_go_to_llm(m, phrase):
    assert m.match(phrase) is None


def test_note_with_polite_word(m):
    # «пожалуйста» перед словом «заметку» не попадает в текст заметки.
    assert m.match("запиши, пожалуйста, заметку купить хлеб") == Intent("add_note", {"text": "купить хлеб"})


def test_chatgpt_and_twitter_voice_aliases_from_config(config):
    matcher = IntentMatcher(
        config.get("windows.apps", {}),
        config.get("windows.websites", {}),
        config.get("windows.folders", {}),
    )
    assert matcher.match("Открой ChatGPT") == Intent("open_website", {"name": "chatgpt"})
    assert matcher.match("открой чатгпт") == Intent("open_website", {"name": "чатгпт"})
    assert matcher.match("открой Twitter") == Intent("open_website", {"name": "twitter"})
    assert matcher.match("открой твиттер") == Intent("open_website", {"name": "твиттер"})
