import pytest

from jarvis.intents import Intent, IntentMatcher

APPS = {"блокнот": "notepad.exe", "диспетчер задач": "taskmgr.exe"}
SITES = {"ютуб": "https://www.youtube.com", "почта": "https://mail.google.com"}


@pytest.fixture
def m():
    return IntentMatcher(APPS, SITES)


@pytest.mark.parametrize("phrase,intent", [
    ("Открой блокнот", Intent("open_app", {"name": "блокнот"})),
    ("Откроет Ютуб", Intent("open_website", {"name": "ютуб"})),
    ("Открой YouTube.", Intent("open_website", {"name": "ютуб"})),
    ("открой ютюб", Intent("open_website", {"name": "ютуб"})),
    ("открой ютубе", Intent("open_website", {"name": "ютуб"})),
    ("Открой почту.", Intent("open_website", {"name": "почта"})),
    ("Джарвис открою YouTube", Intent("open_website", {"name": "ютуб"})),
    ("Подкрой ютуб", Intent("open_website", {"name": "ютуб"})),
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
    "всем привет", "напомни всем позвонить", "от мини", "я ты он", "ставь чайник",
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
    ("Поставь будильник на семь тридцать.", Intent("set_reminder", {"text": "Будильник", "at_time": "07:30"})),
    ("Разбуди меня в шесть сорок пять.", Intent("set_reminder", {"text": "Будильник", "at_time": "06:45"})),
    ("будильник на семь вечера", Intent("set_reminder", {"text": "Будильник", "at_time": "19:00"})),
    ("Напомни в семь вечера купить хлеб.", Intent("set_reminder", {"text": "купить хлеб", "at_time": "19:00"})),
    ("Таймер на полторы минуты.", Intent("set_timer", {"seconds": 90})),
    ("Сколько будет двести пятьдесят умножить на четыре?", Intent("calculate", {"expression": "250*4"})),
    ("Сколько будет пятнадцать процентов от двух тысяч?", Intent("calculate", {"expression": "2000*15/100"})),
    ("Добавь в заметки позвонить врачу.", Intent("add_note", {"text": "позвонить врачу."})),
    ("Какая загрузка процессора?", Intent("system_info")),
    ("Сверни все окна.", Intent("press_keys", {"keys": "show_desktop"})),
    ("Говори голосом Ксении.", Intent("set_voice", {"voice": "kseniya"})),
    ("говори мужским голосом", Intent("set_voice", {"voice": "male"})),
    # Как Whisper слышит команды голосом Silero (scripts/benchmark_speech.py)
    ("Напомни всем вечера купить хлеб", Intent("set_reminder", {"text": "купить хлеб", "at_time": "19:00"})),
    ("от мини таймер.", Intent("cancel_reminders")),
    ("с копируй", Intent("press_keys", {"keys": "copy"})),
    ("за гугли курс доллара", Intent("web_search", {"query": "курс доллара", "site": "google"})),
    ("Ставь!", Intent("press_keys", {"keys": "paste"})),
    ("Отмени последние действия", Intent("press_keys", {"keys": "undo"})),
    ("Добавь заметки позвонить врачу", Intent("add_note", {"text": "позвонить врачу"})),
    ("Сделай покромче", Intent("media_control", {"action": "volume_up"})),
    ("прибавь громкости", Intent("media_control", {"action": "volume_up"})),
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


def test_benchmark_speech_phrases_are_understood_as_text():
    # scripts/benchmark_speech.py проверяет их голосом; без распознавания они обязаны проходить.
    import importlib.util

    from conftest import ROOT
    from jarvis.config import Config
    spec = importlib.util.spec_from_file_location("benchmark_speech", ROOT / "scripts" / "benchmark_speech.py")
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)
    cfg = Config.load(ROOT / "config.toml")
    matcher = IntentMatcher(cfg.get("windows.apps", {}), cfg.get("windows.websites", {}),
                            cfg.get("windows.folders", {}))
    wrong = [c[0] for c in bench.SPOKEN_COMMANDS if not bench.command_understood(matcher, c[0], c)]
    assert not wrong
    assert len(bench.SPOKEN_COMMANDS) >= 50
