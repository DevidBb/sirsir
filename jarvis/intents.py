"""Быстрые команды без LLM.

Частые простые фразы («открой блокнот», «который час») разбираются шаблонами
и сразу превращаются в вызов инструмента из реестра — это быстрее и надёжнее,
чем спрашивать модель. Всё, что не распознано, уходит в LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .app_finder import FILE_EXTENSIONS, SEARCH_SITE_RE
from .calculator import phrase_to_expression
from .reminders import parse_duration
from .security import normalize


@dataclass
class Intent:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)


OPEN_RE = re.compile(r"^(открой|запусти|включи|открыть|запустить|зайди на|перейди на|зайди в)(?: мне)? (.+)$")
# «Включи …» — чаще про музыку и режимы, поэтому «что угодно» открываем только этими глаголами.
OPEN_ANY_VERBS = ("открой", "запусти", "открыть", "запустить", "зайди на", "перейди на", "зайди в")
OPEN_KIND_WORDS = {"сайт": "site", "приложение": "app", "программу": "app", "игру": "app",
                   "папку": "folder", "файл": "file", "документ": "file"}
# Адрес сайта в исходной фразе (после нормализации точки пропадают): «habr.com», «vk.com/feed».
DOMAIN_RE = re.compile(r"(?:https?://)?[\w-]+(?:\.[\w-]+)+(?:/[^\s,!?]*)?", re.I)
# Это не «открой что угодно»: окно, вкладку и т.п. открывают другие команды или LLM.
OPEN_ANY_STOP = {"окно", "вкладку", "новую вкладку", "глаза", "дверь", "секрет"}
# Вежливые слова не меняют смысла команды: «открой, пожалуйста, блокнот».
POLITE_RE = re.compile(r"\b(?:пожалуйста|плиз)\b")
# Приветствие и обращение в начале: «привет, открой ютуб», «слушай, джарвис, который час».
LEAD_RE = re.compile(r"^(?:(?:привет|здравствуй|здорово|слушай|эй|окей|ок|джарвис) )+(?=\S)")
TIME_PHRASES = ("который час", "сколько времени", "какое время", "какое сегодня число",
                "какая сегодня дата", "какое число", "который сейчас час", "сколько сейчас времени",
                "сколько время", "сколько сейчас время", "скажи время", "подскажи время",
                "какой сегодня день", "какой сегодня день недели", "какой день недели",
                "какое сегодня число и день недели")
SYSINFO_PHRASES = ("состояние системы", "загрузка системы", "системная информация",
                   "как там компьютер", "сколько свободно памяти")
TIMER_RE = re.compile(r"^(?:поставь |заведи |запусти |включи )?таймер (.+)$|^засеки(?: таймер)? (.+)$")
# «Поставь будильник на 7:30» — Whisper пишет «7:30», после нормализации «7 30».
ALARM_RE = re.compile(r"^(?:поставь |заведи )?будильник (?:на )?(\d{1,2}) (\d{2})$|^разбуди меня в (\d{1,2}) (\d{2})$")
REMIND_IN_RE = re.compile(r"^напомни(?: мне)? через (.+)$")
REMIND_AT_RE = re.compile(r"^напомни(?: мне)? в (\d{1,2}) (\d{2}) (?:что |чтобы |о том что )?(.+)$")
LIST_REMINDERS = ("какие напоминания", "мои напоминания", "какие таймеры", "список напоминаний",
                  "сколько осталось на таймере", "что с таймером")
CANCEL_REMINDERS_RE = re.compile(r"^(?:отмени|удали|сбрось|выключи|останови) (?:все )?(?:таймеры?|напоминания?)$")
POWER_PATTERNS = [
    (re.compile(r"^(?:заблокируй|заблокировать|блокируй) (?:компьютер|комп|ноутбук|экран|пк)$"), "lock_screen"),
    (re.compile(r"^(?:выключи|выключить|отключи) (?:компьютер|комп|ноутбук|пк)$"), "shutdown_computer"),
    (re.compile(r"^(?:перезагрузи|перезагрузить|перезапусти) (?:компьютер|комп|ноутбук|пк)$"), "restart_computer"),
    (re.compile(r"^(?:переведи (?:компьютер |пк )?в (?:сон|спящий режим)|усыпи (?:компьютер|пк))$"),
     "sleep_computer"),
    (re.compile(r"^(?:отмени|отменить|останови) (?:выключение|перезагрузку)$"), "cancel_shutdown"),
]
NOTE_RE = re.compile(r"^(?:запиши|сделай|создай|добавь) (?:мне )?заметку (?:что |о том что )?(.+)$|^заметка (.+)$")
READ_NOTES = ("прочитай заметки", "какие у меня заметки", "мои заметки", "покажи заметки",
              "что в заметках", "прочитай мои заметки")
CALC_START = re.compile(r"^(?:сколько будет|посчитай|вычисли|корень|квадратный корень)\b")
MEDIA_PATTERNS = [
    (re.compile(r"(?:сделай )?(?:звук |музыку )?(?:по)?громче(?: звук| музыку)?"
                r"|(?:прибавь|увеличь|подними) (?:звук|громкость)|громкость (?:выше|больше)"), "volume_up"),
    (re.compile(r"(?:сделай )?(?:звук |музыку )?(?:по)?тише(?: звук| музыку)?"
                r"|(?:убавь|уменьши|понизь|снизь) (?:звук|громкость)|громкость (?:ниже|меньше)"), "volume_down"),
    (re.compile(r"(?:выключи|отключи|включи) звук|без звука"), "mute"),
    (re.compile(r"пауза|(?:поставь )?(?:музыку |видео )?на паузу|останови музыку"
                r"|(?:продолжи|возобнови) (?:воспроизведение|музыку)"), "play_pause"),
    (re.compile(r"(?:включи |поставь )?следующ(?:ий трек|ую песню|ая песня)|дальше трек"
                r"|(?:переключи|пропусти) (?:трек|песню)"), "next"),
    (re.compile(r"(?:включи |поставь |верни )?(?:предыдущ(?:ий трек|ую песню|ая песня)"
                r"|прошлый трек|прошлую песню)"), "previous"),
]


# Голосовые сочетания клавиш (только целая реплика). «Отмена»/«отмени» — встроенная
# отмена диалога (assistant.BUILTIN_PHRASES), поэтому Ctrl+Z — «отмени ввод», «верни как было».
HOTKEY_PHRASES = {
    "new_line": ("новая строка", "с новой строки", "перенос строки", "новый абзац"),
    "save": ("сохрани", "сохранить", "сохрани файл", "сохрани документ"),
    "undo": ("отмени ввод", "отменить ввод", "верни как было", "отмени последнее действие",
             "отмени изменения"),
    "select_all": ("выдели все", "выделить все", "выдели весь текст"),
    "copy": ("копировать", "скопируй", "копируй", "скопировать"),
    "paste": ("вставить", "вставь", "вставь текст"),
    "close_tab": ("закрой вкладку", "закрыть вкладку"),
    "new_tab": ("новая вкладка", "открой новую вкладку", "новую вкладку"),
}
# Enter — отдельно: отправка сообщения только после «подтверждаю».
SEND_PHRASES = ("отправь", "отправить", "отправь сообщение", "отправить сообщение", "отправляй")
# «Напиши в телеграм привет» — исходный текст, чтобы сохранить регистр и знаки.
TYPE_INTO_RE = re.compile(r"^\s*напиши\s+в\s+(?:окн[оеа]\s+|программ[уе]\s+|приложени[ие]\s+)?"
                          r"([^\s,.:!?—]+)[\s,.:!?—-]+(\S.*)$", re.I | re.S)
COMPOSE_RE = re.compile(
    r"^(?:напиши|сочини|составь|придумай|подготовь|создай|сделай|сгенерируй|разработай)"
    r"(?: мне| пожалуйста| за меня)? "
    r"(?:письмо|сообщение|ответ|текст|промпт|prompt|инструкцию|запрос|описание|сценарий|план|"
    r"поздравление|пост|заявление|отзыв|комментарий|объявление|записку|тост|стих|"
    r"стихотворение|благодарность|извинение|приглашение|просьбу|жалобу)\b", re.I)


# Смена голоса: «смени голос», «голос бая», «женский голос», «поставь голос евгений».
VOICE_NAMES = {"айдар": "aidar", "евгений": "eugene", "женя": "eugene", "бая": "baya", "байя": "baya",
               "ксения": "kseniya", "ксюша": "xenia"}
NEXT_VOICE = ("смени голос", "другой голос", "следующий голос", "поменяй голос", "измени голос")
VOICE_RE = re.compile(r"^(?:(?:поставь|включи|сделай|давай|выбери|смени на|поменяй на) )?"
                      r"(?:(мужской|женский) голос|голос (\w+))$")


# Поиск: «найди на ютубе котиков», «открой ютуб и включи lofi», «загугли погоду»,
# «включи песню Imagine Dragons».
SEARCH_VERB = r"(?:найди|найти|поищи|ищи|покажи|включи|поставь|открой|запусти|посмотри)"
SEARCH_ON_RE = re.compile(rf"^{SEARCH_VERB}(?: мне)? (?:на|в|во) (?:{SEARCH_SITE_RE}) (?P<q>.+)$")
SEARCH_AFTER_OPEN_RE = re.compile(
    rf"^(?:открой|запусти|зайди на|зайди в|перейди на) (?:{SEARCH_SITE_RE})"
    rf" (?:и|и там|а там|и потом) {SEARCH_VERB}(?: мне)? (?P<q>.+)$")
WEB_SEARCH_RE = re.compile(r"^(?:загугли|погугли|гугли)(?: мне)? (?P<g>.+)$"
                           r"|^(?:найди|поищи|ищи|посмотри) в (?:интернете|инете|сети) (?P<d>.+)$")
# English: «search youtube for X», «find X on youtube», «open youtube and search X», «google X».
EN_YT_RE = re.compile(r"^(?:open youtube and )?(?:search|find|look up|look for|show me)(?: me)? "
                      r"(?:youtube for |on youtube |videos? (?:about|on) )(?P<q>.+)$"
                      r"|^(?:search|find|look up) (?P<q2>.+) on youtube$")
EN_WEB_RE = re.compile(r"^(?:google|search the web for|search the internet for|search online for|"
                       r"look up online|find online|search for) (?P<q>.+)$")
# «Запомни, что я люблю короткие ответы», «remember that my name is David».
REMEMBER_RE = re.compile(r"^\s*(?:(?:привет|джарвис|слушай)[\s,!.]+)*(?:запомни|запомните|запиши в память|"
                         r"remember)[\s,:]+(?:(?:что|that)[\s,:]+)?(?P<f>\S.*)$", re.I | re.S)
PREFERENCE_RE = re.compile(r"\b(?:предпочита|люблю|не люблю|нравится|не нравится|удобнее|хочу чтобы|"
                           r"всегда|никогда|обычно|отвечай|говори|пиши|prefer|always|never|i like|i don)", re.I)
# «Посмотри на экран и скажи, что за ошибка», «что у меня на экране», «look at my screen».
LOOK_SCREEN_RE = re.compile(
    r"^(?:посмотри|глянь|взгляни|смотри)(?: на)? (?:мой |наш )?экран\b"
    r"|^(?:что|кто) (?:у меня |сейчас )?(?:на|происходит на) экране\b"
    r"|^(?:объясни|расскажи|опиши|скажи) (?:мне )?что (?:у меня |сейчас )?(?:на|происходит на) экране\b"
    r"|^look at (?:my|the) screen\b|^what(?: is|s| s) on (?:my|the) screen\b")
SCREENSHOT_PHRASES = ("сделай скриншот", "сделай скриншот экрана", "скриншот экрана", "сними экран",
                      "сделай снимок экрана", "снимок экрана", "take a screenshot", "screenshot")
# «Открой второе видео», «открой результат 3», «open the second result».
RESULT_ORDINALS = {"первое": 1, "первый": 1, "первую": 1, "второе": 2, "второй": 2, "вторую": 2,
                   "третье": 3, "третий": 3, "третью": 3, "четвертое": 4, "четвертый": 4,
                   "четвертую": 4, "пятое": 5, "пятый": 5, "пятую": 5, "последнее": -1,
                   "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}
RESULT_WORDS = r"(?:видео|ролик|видос|результат|ссылку|сайт|источник|video|result|link)"
OPEN_RESULT_RE = re.compile(
    rf"^(?:открой|включи|покажи|запусти|open|play|show)(?: мне)?(?: the)? "
    rf"(?:(?P<w>{'|'.join(RESULT_ORDINALS)}) {RESULT_WORDS}|{RESULT_WORDS} (?:номер )?(?P<n>\d+)"
    rf"|(?:номер )?(?P<n2>\d+) {RESULT_WORDS})$")
MUSIC_RE = re.compile(r"^(?:включи|поставь|найди)(?: мне)? (?:песню|трек|музыку|клип|видео) (?P<q>.+)$")


def _lookup(name: str, table: dict[str, str]) -> str | None:
    q = normalize(name)
    for key in table:
        if normalize(key) == q:
            return key
    return None


class IntentMatcher:
    def __init__(self, apps: dict[str, str], websites: dict[str, str],
                 folders: dict[str, str] | None = None, music_site: str = "youtube"):
        self.apps = apps
        self.websites = websites
        self.folders = folders or {}
        self.music_site = music_site

    def match(self, text: str) -> Intent | None:
        norm = LEAD_RE.sub("", normalize(POLITE_RE.sub(" ", normalize(text))))
        if not norm:
            return None
        for action, phrases in HOTKEY_PHRASES.items():
            if norm in phrases:
                return Intent("press_keys", {"keys": action})
        if norm in SEND_PHRASES:
            return Intent("send_message")
        if norm in NEXT_VOICE:
            return Intent("set_voice", {"voice": "next"})
        m = VOICE_RE.match(norm)
        if m:
            if m.group(1):
                return Intent("set_voice", {"voice": "male" if m.group(1) == "мужской" else "female"})
            if m.group(2) in VOICE_NAMES:
                return Intent("set_voice", {"voice": VOICE_NAMES[m.group(2)]})
        m = TYPE_INTO_RE.match(text)
        if m:
            return Intent("type_in_window", {"window": m.group(1), "text": m.group(2).strip()})
        if COMPOSE_RE.match(norm):
            return Intent("compose_text", {"request": text.strip()})
        if LOOK_SCREEN_RE.match(norm):
            return Intent("look_at_screen", {"question": text.strip()})
        if norm in SCREENSHOT_PHRASES:
            return Intent("screen_screenshot")
        m = OPEN_RESULT_RE.match(norm)
        if m:
            index = RESULT_ORDINALS[m.group("w")] if m.group("w") else int(m.group("n") or m.group("n2"))
            return Intent("open_result", {"index": index})
        search = self._match_search(norm)
        if search is not None:
            return search
        reminder = self._match_reminder(norm)
        if reminder is not None:
            return reminder
        m = REMEMBER_RE.match(text)
        if m and not norm.startswith(("запомни где", "запомни когда")):
            fact = m.group("f").strip().rstrip(".!")
            kind = "preference" if PREFERENCE_RE.search(fact) else "fact"
            return Intent("remember_fact", {"fact": fact[:1].upper() + fact[1:], "kind": kind})
        m = NOTE_RE.match(norm)
        if m:
            # Текст заметки берём из исходной фразы, чтобы сохранить регистр и знаки.
            body = m.group(1) or m.group(2)
            original = re.split(r"(?i)заметк[ау]\s*(?:что\s+|о том что\s+)?", text.strip(), maxsplit=1)
            note = original[1].strip(" ,:") if len(original) == 2 and original[1].strip() else body
            return Intent("add_note", {"text": note})
        if norm in READ_NOTES:
            return Intent("read_notes")
        if CALC_START.match(norm):
            expression = phrase_to_expression(text)
            if expression:
                return Intent("calculate", {"expression": expression})
        if len(norm.split()) > 7:
            return None  # длинные фразы — пусть разбирается LLM
        if norm in TIME_PHRASES:
            return Intent("current_time")
        if norm in SYSINFO_PHRASES:
            return Intent("system_info")
        for pattern, tool in POWER_PATTERNS:
            if pattern.match(norm):
                return Intent(tool)
        for pattern, action in MEDIA_PATTERNS:
            if pattern.fullmatch(norm):
                return Intent("media_control", {"action": action})
        m = OPEN_RE.match(norm)
        if m:
            raw_target = m.group(2)
            target = re.sub(r"^(?:сайт|приложение|программу|папку) ", "", raw_target).strip()
            app = _lookup(target, self.apps)
            if app:
                return Intent("open_app", {"name": app})
            site = _lookup(target, self.websites)
            if site:
                return Intent("open_website", {"name": site})
            folder = _lookup(target, self.folders)
            if folder:
                return Intent("open_folder", {"name": folder})
            if m.group(1) in OPEN_ANY_VERBS:
                return self._open_anything(text, raw_target)
        return None

    def _match_search(self, norm: str) -> Intent | None:
        if any(pattern.fullmatch(norm) for pattern, _ in MEDIA_PATTERNS):
            return None  # «поставь музыку на паузу» — это пауза, а не поиск
        for regex in (SEARCH_AFTER_OPEN_RE, SEARCH_ON_RE):
            m = regex.match(norm)
            if m:
                site = next(k for k, v in m.groupdict().items() if v and k != "q")
                return Intent("web_search", {"query": m.group("q"), "site": site})
        m = WEB_SEARCH_RE.match(norm)
        if m:
            if m.group("g"):
                return Intent("web_search", {"query": m.group("g"), "site": "google"})
            return Intent("web_search", {"query": m.group("d")})
        m = MUSIC_RE.match(norm)
        if m:
            return Intent("web_search", {"query": m.group("q"), "site": self.music_site})
        m = EN_YT_RE.match(norm)
        if m:
            return Intent("web_search", {"query": m.group("q") or m.group("q2"), "site": "youtube"})
        m = EN_WEB_RE.match(norm)
        if m:
            return Intent("web_search", {"query": m.group("q"), "site": "default"})
        return None

    @staticmethod
    def _open_anything(text: str, target: str) -> Intent | None:
        """«Открой телеграм», «зайди на habr.com», «открой папку отчёты» — без белого списка."""
        kind = "any"
        first, _, rest = target.partition(" ")
        if first in OPEN_KIND_WORDS and rest:
            kind, target = OPEN_KIND_WORDS[first], rest
        if target in OPEN_ANY_STOP or not target:
            return None
        domain = DOMAIN_RE.search(text)
        if (domain and kind in ("any", "site") and not domain.group(0)[0].isdigit()
                and domain.group(0).rstrip(".").rsplit(".", 1)[-1].lower() not in FILE_EXTENSIONS):
            return Intent("open_anything", {"name": domain.group(0).rstrip("."), "kind": "site"})
        args: dict[str, Any] = {"name": target}
        if kind != "any":
            args["kind"] = kind
        return Intent("open_anything", args)

    @staticmethod
    def _match_reminder(norm: str) -> Intent | None:
        if norm in LIST_REMINDERS:
            return Intent("list_reminders")
        if CANCEL_REMINDERS_RE.match(norm):
            return Intent("cancel_reminders")
        m = TIMER_RE.match(norm)
        if m:
            seconds = parse_duration(m.group(1) or m.group(2))
            return Intent("set_timer", {"seconds": seconds}) if seconds else None
        m = ALARM_RE.match(norm)
        if m:
            hour, minute = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            if int(hour) <= 23 and int(minute) <= 59:
                return Intent("set_reminder", {"text": "Будильник", "at_time": f"{int(hour):02d}:{minute}"})
            return None
        m = REMIND_AT_RE.match(norm)
        if m:
            return Intent("set_reminder", {"text": m.group(3),
                                           "at_time": f"{int(m.group(1)):02d}:{m.group(2)}"})
        m = REMIND_IN_RE.match(norm)
        if m:
            # «через двадцать пять минут позвонить маме»: ищем самое длинное
            # начало, которое читается как длительность.
            words = m.group(1).split()
            for k in range(min(6, len(words) - 1), 0, -1):
                seconds = parse_duration(" ".join(words[:k]))
                if seconds:
                    rest = re.sub(r"^(?:что |чтобы |о том что |про )", "", " ".join(words[k:]) + " ").strip()
                    if rest and seconds % 60 == 0:
                        return Intent("set_reminder", {"text": rest, "minutes": seconds // 60})
                    # «через 1 минуту 30 секунд ...» — не отрезаем секунды в текст, пусть решает LLM.
                    return None
            return None
        return None
