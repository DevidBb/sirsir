"""Подготовка текста для нейросетевого TTS: числа, время и символы — словами.

Silero не читает цифры, поэтому «12:43» → «двенадцать сорок три»,
«15 %» → «пятнадцать процентов», «2.5» → «два и пять».
"""

from __future__ import annotations

import re

UNITS_M = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
UNITS_F = ["ноль", "одна", "две"] + UNITS_M[3:]
TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
         "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать"]
TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят",
        "восемьдесят", "девяносто"]
HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот",
            "восемьсот", "девятьсот"]
SCALES = [  # (значение, формы, женский род)
    (10**9, ("миллиард", "миллиарда", "миллиардов"), False),
    (10**6, ("миллион", "миллиона", "миллионов"), False),
    (10**3, ("тысяча", "тысячи", "тысяч"), True),
]


def plural(n: int, forms: tuple[str, str, str]) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def _below_thousand(n: int, feminine: bool) -> list[str]:
    words = []
    if n >= 100:
        words.append(HUNDREDS[n // 100])
        n %= 100
    if 10 <= n <= 19:
        words.append(TEENS[n - 10])
        return words
    if n >= 20:
        words.append(TENS[n // 10])
        n %= 10
    if n:
        words.append((UNITS_F if feminine else UNITS_M)[n])
    return words


def number_to_words(n: int, feminine: bool = False) -> str:
    if n == 0:
        return "ноль"
    if n < 0:
        return "минус " + number_to_words(-n, feminine)
    if n >= 10**12:
        return " ".join(UNITS_M[int(d)] for d in str(n))  # читаем по цифрам
    words: list[str] = []
    for value, forms, fem in SCALES:
        if n >= value:
            chunk, n = divmod(n, value)
            if not (chunk == 1 and value == 10**3):  # «тысяча», а не «одна тысяча»
                words += _below_thousand(chunk, fem)
            words.append(plural(chunk, forms))
    words += _below_thousand(n, feminine)
    return " ".join(words)


def _time(m: re.Match) -> str:
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return m.group(0)
    hours = number_to_words(h)
    if mi == 0:
        return f"{hours} ноль ноль"
    minutes = number_to_words(mi, feminine=True)
    return f"{hours} ноль {minutes}" if mi < 10 else f"{hours} {minutes}"


def _decimal(m: re.Match) -> str:
    return f"{number_to_words(int(m.group(1)))} и {number_to_words(int(m.group(2)), feminine=True)}"


def _percent(m: re.Match) -> str:
    n = int(m.group(1))
    return f"{number_to_words(n)} {plural(n, ('процент', 'процента', 'процентов'))}"


SYMBOLS = {"&": " и ", "+": " плюс ", "=": " равно ", "№": " номер ", "°": " градусов ",
           "€": " евро ", "$": " долларов ", "₽": " рублей "}


def normalize_for_tts(text: str) -> str:
    text = re.sub(r"\b(\d{1,2}):(\d{2})\b", _time, text)
    text = re.sub(r"\b(\d+)\s?%", _percent, text)
    text = re.sub(r"\b(\d+)[.,](\d+)\b", _decimal, text)
    text = re.sub(r"\d+", lambda m: number_to_words(int(m.group(0))), text)
    for sym, word in SYMBOLS.items():
        text = text.replace(sym, word)
    text = re.sub(r"[*_#`<>\[\]{}|\\^~]", " ", text)
    return " ".join(text.split())
