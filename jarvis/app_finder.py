"""Поиск установленных программ по ярлыкам меню «Пуск» и адресов сайтов.

Безопасность:
- запускается только сам найденный ярлык (.lnk/.url) через os.startfile, без оболочки;
- ярлык не запускается, если ведёт на скрипт (.bat/.cmd/.ps1/.vbs…), на программу
  из загрузок или временной папки, или на скриптовый хост с аргументами;
- .url-ярлыки — только с разрешёнными схемами (http/https, steam:// и т.п.);
- сайты открываются только по http/https в браузере (webbrowser), страницы
  помощник сам не загружает.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Callable, Iterable
from urllib.parse import urlsplit

from .fuzzy import similarity, translit
from .security import normalize

SCRIPT_EXTS = {".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh",
               ".hta", ".scr", ".pif", ".com", ".reg", ".jar", ".lnk", ".url"}
# Программы и оснастки (.msc — «Управление компьютером» и т.п.): из меню «Пуск» можно,
# если лежат не в загрузках/temp; из папок пользователя не открываются никогда.
PROGRAM_EXTS = {".exe", ".msi", ".msix", ".appx", ".appinstaller", ".application", ".appref-ms",
                ".msc", ".cpl"}
RISKY_DIR_NAMES = {"downloads", "загрузки", "temp", "tmp", "inetcache", "temporary internet files"}
SCRIPT_HOSTS = {"cmd.exe", "powershell.exe", "pwsh.exe", "wscript.exe", "cscript.exe", "mshta.exe",
                "rundll32.exe", "regsvr32.exe", "bash.exe", "wsl.exe"}
DEFAULT_URL_SCHEMES = ["http", "https", "steam", "com.epicgames.launcher", "uplay", "origin2",
                       "battlenet", "ms-settings"]
# Служебные ярлыки, которые не нужны при «открой …».
JUNK_WORDS = {"uninstall", "удалить", "удаление", "деинсталляция", "readme", "help", "справка",
              "документация", "documentation", "manual", "license", "лицензия", "changelog",
              "website"}
MAX_SHORTCUTS = 5000


def default_start_menu_dirs() -> list[Path]:
    dirs = []
    for var in ("ProgramData", "APPDATA"):
        base = os.environ.get(var)
        if base:
            dirs.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return dirs


# --- разбор ярлыков ----------------------------------------------------------
def _u16(b: bytes, i: int) -> int:
    return int.from_bytes(b[i:i + 2], "little")


def _u32(b: bytes, i: int) -> int:
    return int.from_bytes(b[i:i + 4], "little")


def _cstr(b: bytes, i: int, wide: bool) -> str:
    if wide:
        end = i
        while end + 1 < len(b) and b[end:end + 2] != b"\0\0":
            end += 2
        return b[i:end].decode("utf-16-le", "replace")
    end = b.find(b"\0", i)
    return b[i:end if end >= 0 else len(b)].decode("mbcs" if os.name == "nt" else "cp1251", "replace")


def parse_lnk(data: bytes) -> tuple[str | None, str]:
    """(путь к цели или None, аргументы) из двоичного .lnk (MS-SHLLINK), без COM."""
    if len(data) < 0x4C or data[:4] != b"\x4c\0\0\0":
        return None, ""
    flags = _u32(data, 0x14)
    pos = 0x4C
    target: str | None = None
    try:
        if flags & 0x01:  # HasLinkTargetIDList
            pos += 2 + _u16(data, pos)
        if flags & 0x02:  # HasLinkInfo
            size, header, li_flags = _u32(data, pos), _u32(data, pos + 4), _u32(data, pos + 8)
            if li_flags & 0x01:  # VolumeIDAndLocalBasePath
                if header >= 0x24:
                    target = _cstr(data, pos + _u32(data, pos + 0x1C), True)
                else:
                    target = _cstr(data, pos + _u32(data, pos + 0x10), False)
            pos += size
        wide = bool(flags & 0x80)
        strings = {}
        for bit, key in ((0x04, "name"), (0x08, "relative"), (0x10, "workdir"),
                         (0x20, "args"), (0x40, "icon")):
            if flags & bit:
                count = _u16(data, pos)
                raw = data[pos + 2: pos + 2 + count * (2 if wide else 1)]
                strings[key] = raw.decode("utf-16-le" if wide else "cp1251", "replace")
                pos += 2 + count * (2 if wide else 1)
        # ExtraData: EnvironmentVariableDataBlock (цель вида %ProgramFiles%\…).
        while pos + 8 <= len(data):
            size = _u32(data, pos)
            if size < 8:
                break
            if _u32(data, pos + 4) == 0xA0000001 and not target:
                env_target = _cstr(data, pos + 8 + 260, True) or _cstr(data, pos + 8, False)
                target = os.path.expandvars(env_target) or None
            pos += size
        if not target and strings.get("relative"):
            target = strings["relative"]
        return target or None, strings.get("args", "")
    except (IndexError, ValueError):
        return target, ""


def parse_url_file(text: str) -> str | None:
    for line in text.splitlines():
        if line.strip().lower().startswith("url="):
            return line.split("=", 1)[1].strip()
    return None


def _win_parts(path: str) -> list[str]:
    return [p.lower() for p in PureWindowsPath(path).parts]


def unsafe_target_reason(target: str | None, args: str = "") -> str | None:
    """Почему цель ярлыка запускать нельзя (или None, если можно)."""
    if not target:
        return None  # «рекламные» ярлыки MSI без пути — обычные установленные программы
    name = PureWindowsPath(target).name.lower()
    ext = PureWindowsPath(target).suffix.lower()
    if ext in SCRIPT_EXTS:
        return "ярлык ведёт на скрипт"
    if ext in PROGRAM_EXTS and RISKY_DIR_NAMES.intersection(_win_parts(target)):
        return "ярлык ведёт на программу из загрузок или временной папки"
    if name in SCRIPT_HOSTS and args.strip():
        return "ярлык запускает командную строку с аргументами"
    return None


def is_risky_file(path: Path) -> bool:
    """Исполняемые файлы и скрипты не открываем «двойным щелчком»."""
    return path.suffix.lower() in SCRIPT_EXTS | PROGRAM_EXTS


# --- индекс ярлыков ----------------------------------------------------------
@dataclass(frozen=True)
class Shortcut:
    name: str
    path: Path


class StartMenuIndex:
    """Список ярлыков меню «Пуск» с кэшем (пересканируется раз в cache_seconds)."""

    def __init__(self, dirs: Iterable[Path] | None = None, cache_seconds: float = 600,
                 clock: Callable[[], float] = time.monotonic):
        self.dirs = [Path(d) for d in dirs] if dirs is not None else default_start_menu_dirs()
        self.cache_seconds = cache_seconds
        self.clock = clock
        self._cache: list[Shortcut] | None = None
        self._loaded_at = 0.0
        self.scans = 0

    def refresh(self) -> None:
        self._cache = None

    def shortcuts(self) -> list[Shortcut]:
        if self._cache is None or self.clock() - self._loaded_at > self.cache_seconds:
            self._cache = self._scan()
            self._loaded_at = self.clock()
        return self._cache

    def _scan(self) -> list[Shortcut]:
        self.scans += 1
        found: dict[str, Shortcut] = {}
        for base in self.dirs:
            if not base.is_dir():
                continue
            for root, _dirs, files in os.walk(base, followlinks=False):
                for fname in files:
                    p = Path(root) / fname
                    if p.suffix.lower() not in (".lnk", ".url"):
                        continue
                    key = normalize(p.stem)
                    if not key or JUNK_WORDS.intersection(key.split()) or "веб сайт" in key:
                        continue
                    found.setdefault(key, Shortcut(p.stem, p))  # одинаковые у всех и у себя
                    if len(found) >= MAX_SHORTCUTS:
                        return list(found.values())
        return list(found.values())

    def search(self, query: str, threshold: float = 0.75) -> list[tuple[float, Shortcut]]:
        scored = [(similarity(query, s.name), s) for s in self.shortcuts()]
        return sorted([x for x in scored if x[0] >= threshold], key=lambda x: -x[0])


# --- сайты -------------------------------------------------------------------
# Популярные сайты по названию (кроме тех, что заданы в config.toml).
KNOWN_SITES = {
    "вконтакте": "https://vk.com", "вк": "https://vk.com", "в контакте": "https://vk.com",
    "одноклассники": "https://ok.ru", "яндекс": "https://ya.ru", "гугл": "https://www.google.com",
    "хабр": "https://habr.com", "авито": "https://www.avito.ru", "озон": "https://www.ozon.ru",
    "вайлдберриз": "https://www.wildberries.ru", "госуслуги": "https://www.gosuslugi.ru",
    "кинопоиск": "https://www.kinopoisk.ru", "твич": "https://www.twitch.tv",
    "реддит": "https://www.reddit.com", "рутуб": "https://rutube.ru",
    "яндекс карты": "https://yandex.ru/maps", "карты": "https://yandex.ru/maps",
    "яндекс музыка": "https://music.yandex.ru", "переводчик": "https://translate.yandex.ru",
    "погода": "https://yandex.ru/pogoda", "почта яндекс": "https://mail.yandex.ru",
    "яндекс почта": "https://mail.yandex.ru", "чат gpt": "https://chatgpt.com",
    "стек оверфлоу": "https://stackoverflow.com", "дзен": "https://dzen.ru",
    "телеграм веб": "https://web.telegram.org", "ватсап веб": "https://web.whatsapp.com",
}
# Как Whisper и Vosk записывают названия сайтов → имя из config.toml/KNOWN_SITES.
# Нечёткое сравнение их не ловит: «youtube» похоже на «ютуб» только на 0.83.
SITE_ALIASES = {
    "youtube": "ютуб", "you tube": "ютуб", "ютюб": "ютуб", "ютьюб": "ютуб", "ю туб": "ютуб",
    "github": "гитхаб", "git hub": "гитхаб", "гит хаб": "гитхаб",
    "wikipedia": "википедия", "вики": "википедия",
    "chat gpt": "чатгпт", "чат джипити": "чатгпт", "чат гпт": "чатгпт", "чат жпт": "чатгпт",
    "vk": "вконтакте", "яндекс почта": "почта яндекс", "google": "гугл", "yandex": "яндекс",
    "habr": "хабр", "twitch": "твич", "reddit": "реддит", "rutube": "рутуб", "avito": "авито",
    "ozon": "озон", "wildberries": "вайлдберриз", "валберис": "вайлдберриз", "вб": "вайлдберриз",
    "stackoverflow": "стек оверфлоу", "stack overflow": "стек оверфлоу",
}


def site_alias(name: str, table: dict[str, str]) -> str | None:
    """Ключ таблицы сайтов для произнесённого названия: «YouTube», «ютюбе», «википедию»."""
    from .fuzzy import similarity
    from .security import normalize
    q = normalize(name)
    alias = SITE_ALIASES.get(q)
    for key in table:
        if normalize(key) == alias:
            return key
    # Падежи и мелкие ошибки распознавания: «ютубе», «википедию». Только для слов подлиннее —
    # у коротких имён («икс», «вк») один звук меняет смысл.
    if len(q) >= 4:
        best = max(table, key=lambda k: similarity(q, k), default=None)
        if best is not None and similarity(q, best) >= 0.9:
            return best
    return None

# «отчёт.docx» — это файл, а не сайт.
FILE_EXTENSIONS = {"doc", "docx", "xls", "xlsx", "ppt", "pptx", "pdf", "txt", "rtf", "odt", "csv",
                   "jpg", "jpeg", "png", "gif", "bmp", "webp", "mp3", "mp4", "avi", "mkv", "wav",
                   "flac", "mov", "zip", "rar", "7z", "exe", "msi", "bat", "cmd", "ps1", "lnk",
                   "url", "json", "xml", "html", "htm", "py", "log", "ini", "md", "torrent", "iso"}
# Поиск внутри сайтов и приложений: «найди на ютубе котиков», «включи в спотифае lofi».
# id → (как назвать в ответе, шаблон адреса, как говорят — регулярное выражение по словам).
SEARCH_SITES: dict[str, tuple[str, str, str]] = {
    "youtube": ("YouTube", "https://www.youtube.com/results?search_query={q}", r"ютуб\w*|youtube"),
    "google": ("Google", "https://www.google.com/search?q={q}", r"гугл\w*|google"),
    "yandex": ("Яндекс", "https://ya.ru/search/?text={q}", r"яндекс[еау]?"),
    "yandex_music": ("Яндекс Музыка", "https://music.yandex.ru/search?text={q}",
                     r"яндекс музык\w*"),
    "maps": ("Яндекс Карты", "https://yandex.ru/maps/?text={q}", r"(?:яндекс )?карт\w*"),
    "wikipedia": ("Википедия", "https://ru.wikipedia.org/w/index.php?search={q}",
                  r"википеди\w*|вики"),
    "vk": ("ВКонтакте", "https://vk.com/search?c%5Bq%5D={q}", r"вк|вконтакте|в контакте"),
    "rutube": ("Rutube", "https://rutube.ru/search/?query={q}", r"рутуб\w*"),
    "kinopoisk": ("Кинопоиск", "https://www.kinopoisk.ru/index.php?kp_query={q}", r"кинопоиск\w*"),
    "avito": ("Авито", "https://www.avito.ru/rossiya?q={q}", r"авито"),
    "ozon": ("Озон", "https://www.ozon.ru/search/?text={q}", r"озон\w*"),
    "wildberries": ("Wildberries", "https://www.wildberries.ru/catalog/0/search.aspx?search={q}",
                    r"вайлдберриз\w*|валберис\w*|вб"),
    "github": ("GitHub", "https://github.com/search?q={q}", r"гитхаб\w*|github"),
    "twitch": ("Twitch", "https://www.twitch.tv/search?term={q}", r"твич\w*|twitch"),
    "steam": ("магазин Steam", "https://store.steampowered.com/search/?term={q}", r"стим\w*|steam"),
    # Приложение: адрес spotify: открывает поиск прямо в установленном Spotify.
    "spotify": ("Spotify", "spotify:search:{q}", r"спотифа\w*|spotify"),
}
# Сначала названия из нескольких слов: иначе «яндекс музыке кино» прочиталось бы как «яндекс».
SEARCH_SITE_RE = "|".join(f"(?P<{key}>{spoken})" for key, (_t, _u, spoken) in
                          sorted(SEARCH_SITES.items(), key=lambda kv: " " not in kv[1][2]))


def search_url(site: str, query: str) -> str:
    """Адрес поиска: запрос кодируется целиком, подставить в адрес что-то своё нельзя."""
    from urllib.parse import quote, quote_plus
    _title, template, _spoken = SEARCH_SITES[site]
    q = quote(query, safe="") if template.startswith("spotify:") else quote_plus(query)
    return template.format(q=q)


_SPOKEN_TLDS = {"ком": "com", "ру": "ru", "рф": "рф", "нет": "net", "орг": "org", "ио": "io",
                "тв": "tv", "дев": "dev", "су": "su", "бай": "by", "кз": "kz", "юа": "ua"}
_HOST_RE = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9а-я](?:[a-z0-9а-я-]{0,61}[a-z0-9а-я])?\.)+"
                      r"(?:[a-z]{2,24}|рф|xn--[a-z0-9-]+)$")


def site_url(text: str) -> str | None:
    """«habr.com», «хабр точка ком», «https://habr.com/ru» → https-адрес; иначе None."""
    t = text.strip().lower().replace("ё", "е").rstrip(".,!?;:")
    t = re.sub(r"\s+(?:точка|dot)\s+", ".", t)
    t = re.sub(r"\.(\w+)", lambda m: "." + _SPOKEN_TLDS.get(m.group(1), m.group(1)), t)
    scheme = "https"
    m = re.match(r"^(https?)://", t)
    if m:
        scheme, t = m.group(1), t[m.end():]
    elif "://" in t or t.startswith(("javascript:", "file:", "data:")):
        return None
    if " " in t or "@" in t or "\\" in t:
        return None
    host, _, rest = t.partition("/")
    host = host.split(":")[0]
    if not host.endswith(".рф") and re.search(r"[а-я]", host):
        host = translit(host)  # «хабр точка ком» → habr.com
    if not _HOST_RE.match(host) or host.rsplit(".", 1)[-1] in FILE_EXTENSIONS:
        return None
    url = f"{scheme}://{host}" + (f"/{rest}" if rest else "")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or len(url) > 300:
        return None
    return url


def url_scheme_allowed(url: str, schemes: Iterable[str]) -> bool:
    scheme = url.split(":", 1)[0].lower() if ":" in url else ""
    return scheme in {s.lower() for s in schemes}
