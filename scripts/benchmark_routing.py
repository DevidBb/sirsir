"""Куда уходит каждая фраза в гибридном режиме: быстрая команда, локальная Ollama или Claude.

    python scripts/benchmark_routing.py            # таблица маршрутов, ничего не выполняется
    python scripts/benchmark_routing.py --live     # плюс настоящие ответы и время (Ollama и Claude)

Без --live фразы проходят через настоящий Assistant.handle(), но модели, инструменты, окна и
клавиатура заменены записывающими заглушками: ничего не открывается и не печатается. Так видно
само решение маршрутизации. Ожидаемые маршруты в CASES — правила гибрида:

* fast   — без модели: встроенные и быстрые команды (открыть, найти, таймер, громкость…);
* local  — Ollama: короткие вопросы, разговор, определения, переводы, короткие тексты;
* claude — Claude: свежие данные (погода, новости, цены, «сейчас»), многошаговые задачи
           (найти и сравнить, действия на сайте, файлы проекта), длинный анализ и план,
           снимок экрана; продолжение («дальше», «подробнее») — там же, где был прошлый ответ.

--live (на ПК с Ollama и входом в Claude): каждая фраза маршрутов local/claude выполняется
по-настоящему, инструменты — тоже (браузер может открыться), действия с подтверждением не
выполняются. Печатаются время ответа и начало ответа.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# (фраза, ожидаемый маршрут). Цепочки (продолжение разговора) — кортеж фраз, маршрут последней.
CASES: list[tuple[str | tuple[str, ...], str]] = [
    # --- быстрые команды: без модели ---
    ("который час", "fast"),
    ("открой ютуб", "fast"),
    ("открой блокнот", "fast"),
    ("найди на ютубе обзор rtx 5070", "fast"),
    ("найди в интернете рецепт борща", "fast"),
    ("загугли курс доллара", "fast"),
    ("открой ютуб и найди видео про котиков", "fast"),
    ("поставь таймер на 5 минут", "fast"),
    ("напомни через 10 минут проверить духовку", "fast"),
    ("сколько будет 17 умножить на 3", "fast"),
    ("сделай громче", "fast"),
    ("поставь на паузу", "fast"),
    ("запиши заметку купить молоко", "fast"),
    ("сделай скриншот экрана", "fast"),
    ("выключи микрофон", "fast"),
    ("спасибо", "fast"),
    ("что ты умеешь", "fast"),
    ("открой папку загрузки", "fast"),
    ("запомни, что я люблю короткие ответы", "fast"),
    ("открой блокнот и напиши привет", "fast"),
    # --- обычные вопросы и разговор: локально ---
    ("как дела", "local"),
    ("привет, как настроение", "local"),
    ("что такое рекурсия", "local"),
    ("переведи на английский: доброе утро", "local"),
    ("придумай имя для кота", "local"),
    ("в чём разница между ОЗУ и видеопамятью", "local"),
    ("как приготовить омлет", "local"),
    ("посоветуй фильм на вечер", "local"),
    ("кто написал войну и мир", "local"),
    ("расскажи анекдот", "local"),
    ("расскажи о себе", "local"),
    ("почему небо голубое", "local"),
    ("объясни простыми словами, что такое API", "local"),
    ("напиши короткое поздравление с днём рождения для мамы", "local"),
    ("сколько калорий в банане", "local"),
    ("какая столица Австралии", "local"),
    ("как сказать спасибо по-немецки", "local"),
    ("придумай загадку", "local"),
    ("что лучше для игр: процессор или видеокарта", "local"),
    ("what is the capital of Canada", "local"),
    (("расскажи анекдот", "давай ещё один"), "local"),
    (("что такое рекурсия", "а подробнее?"), "local"),
    # --- свежие данные, многошаговые задачи, анализ: Claude ---
    ("какая погода завтра в Москве", "claude"),
    ("расскажи последние новости", "claude"),
    ("какой сейчас курс доллара", "claude"),
    ("сколько сейчас стоит RTX 5070", "claude"),
    ("кто выиграл последний матч Реала", "claude"),
    ("какие фильмы сейчас идут в кино", "claude"),
    ("найди в интернете, что такое MCP, открой два источника и сравни", "claude"),
    ("сравни iPhone 17 и Samsung S26 и дай вывод", "claude"),
    ("открой en.wikipedia.org, найди статью про Python и скажи, когда он появился", "claude"),
    ("проверь мой проект в D:/Projects/site и найди ошибки", "claude"),
    ("составь план тренировок на неделю", "claude"),
    ("проанализируй мои расходы за месяц из файла expenses.csv", "claude"),
    ("объясни подробно, чем отличается TCP от UDP, и приведи примеры", "claude"),
    ("посмотри на экран и объясни, что за ошибка", "claude"),
    ("найди отзывы о ноутбуке Lenovo Legion 5 и скажи, стоит ли его брать", "claude"),
    ("напиши письмо начальнику о переносе встречи на пятницу и отправь его в телеграм", "claude"),
    ("what's the weather in London tomorrow", "claude"),
    (("какая погода завтра в Москве", "а послезавтра?"), "claude"),
    (("сравни iPhone 17 и Samsung S26 и дай вывод", "продолжай"), "claude"),
    (("какая погода завтра в Москве", "открой ютуб", "а послезавтра?"), "claude"),
    # --- проверочные наборы (сначала замерены без подгонки: 30/40 и 31/39, см. docs/ROADMAP.md) ---
    ("открой телеграм", "fast"),
    ("выключи звук", "fast"),
    ("найди на ютубе как поменять масло в машине", "fast"),
    ("который сейчас час", "fast"),
    ("открой второе видео", "fast"),
    ("закрой вкладку", "fast"),
    ("напомни завтра в 9 позвонить врачу", "fast"),
    ("напомни в 7 вечера купить хлеб", "fast"),
    ("сколько будет 15 процентов от 2400", "fast"),
    ("сколько будет 2 в 10 степени", "fast"),
    ("покажи мои напоминания", "fast"),
    ("открой спотифай", "fast"),
    ("поставь таймер на полчаса", "fast"),
    ("найди в интернете расписание электричек", "fast"),
    ("сделай тише", "fast"),
    ("как тебя зовут", "local"),
    ("придумай название для канала про игры", "local"),
    ("кто такой Илон Маск", "local"),
    ("переведи на русский: good morning", "local"),
    ("дай совет, как быстрее заснуть", "local"),
    ("напиши хокку про осень", "local"),
    ("чем отличается кофе от чая", "local"),
    ("а если по-другому?", "local"),
    ("что сегодня приготовить на ужин", "local"),
    ("мне сейчас скучно, предложи занятие", "local"),
    ("сочини короткий стишок про кота", "local"),
    ("в каком году началась вторая мировая", "local"),
    ("как сделать скриншот на windows", "local"),
    ("давай поговорим о космосе", "local"),
    ("какой сегодня курс евро", "claude"),
    ("что сейчас происходит на бирже", "claude"),
    ("будет ли дождь в выходные", "claude"),
    ("когда выйдет GTA 6", "claude"),
    ("найди лучший ноутбук до 80 тысяч и обоснуй выбор", "claude"),
    ("прочитай статью по ссылке https://example.com и перескажи", "claude"),
    ("напиши скрипт на Python, который переименовывает файлы", "claude"),
    ("помоги разобраться с ошибкой в коде", "claude"),
    ("сделай краткое содержание этой страницы", "claude"),
    ("закажи пиццу", "claude"),
    ("что пишут про новый айфон", "claude"),
    ("переведи эту страницу на русский", "claude"),
    ("найди дешёвые билеты в Сочи на следующие выходные", "claude"),
    ("что посмотреть на нетфликсе из нового", "claude"),
    ("выиграл ли Спартак вчера", "claude"),
    ("проверь, работает ли сайт example.com", "claude"),
    ("сделай выжимку из этого видео", "claude"),
    ("напиши функцию на JavaScript для сортировки массива", "claude"),
    ("узнай, во сколько закрывается ИКЕА сегодня", "claude"),
    ("посоветуй, какую видеокарту купить для игр в 2026 году", "claude"),
    # составные: «открой и сделай» — быстро, «открой и найди, где/как …» — вопрос модели
    ("открой проводник и найди файл отчёт", "fast"),
    ("найди файл договор аренды", "fast"),
    ("где лежит документ резюме", "fast"),
    ("найди файл, где написано про отпуск", "local"),
    ("найди на ютубе как поменять масло в машине", "fast"),
    ("открой мой проект и найди, где задаётся порт сервера", "claude"),
    ("зайди на github и посмотри, какие есть открытые задачи", "claude"),
]


class Recorder:
    """Что вызвала фраза: инструменты, Ollama, Claude."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def route(self) -> str:
        kinds = {kind for kind, _ in self.events}
        return "claude" if "claude" in kinds else "local" if "ollama" in kinds else "fast"

    def detail(self) -> str:
        return ", ".join(name for kind, name in self.events if kind == "tool") or ""


class FakeLLM:
    def __init__(self, rec: Recorder) -> None:
        self.rec = rec
        self.model = "qwen3:8b"

    def chat(self, messages, tools=None, on_text=None, options=None, model=None):
        self.rec.events.append(("ollama", ""))
        return {"role": "assistant", "content": "Ответ локальной модели."}

    def has_model(self) -> bool:
        return True

    def preload(self) -> None:
        pass


class FakeClaude:
    def __init__(self, rec: Recorder) -> None:
        self.rec = rec
        self.session_id = None
        self.last_error = None

    def run(self, assistant, text, on_sentence=None, image=None):
        from jarvis.assistant import Reply
        self.rec.events.append(("claude", ""))
        assistant.conversation.begin_turn(text)
        assistant.conversation.add({"role": "assistant", "content": "Ответ Claude."})
        assistant.conversation.end_turn()
        return Reply("Ответ Claude.")

    def warm_up(self, assistant) -> None:
        pass

    def close(self, wait: float = 0) -> None:
        pass


class FakeDictation:
    """Клавиатура и окна: только запись."""

    def __init__(self, rec: Recorder) -> None:
        self.rec = rec
        self.active = False

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.rec.events.append(("tool", name))
            return "Окно" if name in ("foreground_title", "wait_new_foreground") else None
        return record


def make_config(data_dir: Path):
    from jarvis.config import Config
    config = Config.load(ROOT / "config.toml")  # без config.local.toml: замер не зависит от ПК
    config.set("memory.db_path", str(data_dir / "memory.db"))
    config.set("windows.launcher.start_menu_dirs", [])
    config.set("llm.provider", "hybrid")
    config.set("llm.preload", False)
    config.set("mcp.servers", {})
    return config


def dry_app(config):
    """App с заглушками вместо моделей, инструментов и клавиатуры."""
    from jarvis.assistant import Reply
    from jarvis.main import App
    rec = Recorder()
    app = App(config, voice=False)
    a = app.assistant
    a.llm = app.llm = FakeLLM(rec)
    a.agent_backend = FakeClaude(rec)
    a.dictation = FakeDictation(rec)

    def run_tool(name, args):
        rec.events.append(("tool", name))
        return Reply(f"Открываю {name}." if name.startswith("open") else "Готово.")
    a._run_tool = run_tool
    a._look_at_screen = lambda q: a.agent_backend.run(a, "[снимок экрана] " + q)
    return app, rec


def reset(a) -> None:
    """Новый разговор: без ожидающих подтверждений, выбора, диктовки и антиповтора."""
    a.confirmations.cancel()
    a._pending_choice = None
    a.dictation.active = False
    a.conversation.clear()
    a.guard._last_command = None
    a.guard._actions.clear()
    a.guard._reads.clear()


def route_cases(cases=CASES) -> list[dict]:
    """Маршрут каждой фразы (без --live). Возвращает строки таблицы."""
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        app, rec = dry_app(make_config(Path(tmp)))
        a = app.assistant
        try:
            for phrases, expected in cases:
                chain = (phrases,) if isinstance(phrases, str) else phrases
                reset(a)
                for text in chain:
                    rec.events.clear()
                    a.handle(text)
                rows.append({"text": " → ".join(chain), "expected": expected, "route": rec.route(),
                             "detail": rec.detail(), "skill": getattr(a.active_skill, "name", "")})
        finally:
            app.close()
    return rows


def live(rows: list[dict], limit: int = 0, only: str = "", model: str = "") -> None:
    """Настоящие ответы для фраз local/claude: время и начало ответа."""
    from jarvis.main import App
    config = make_config(ROOT / "data" / "benchmark-routing")
    if model:
        config.set("llm.model", model)
    (ROOT / "data" / "benchmark-routing").mkdir(parents=True, exist_ok=True)
    app = App(config, voice=False)
    picked = [r for r in rows if r["route"] != "fast" and (not only or r["route"] == only)]
    try:
        for row in picked[:limit or None]:
            app.assistant.conversation.clear()
            app.assistant.confirmations.cancel()
            started = time.time()
            reply = None
            for text in row["text"].split(" → "):
                reply = app.assistant.handle(text)
            took = time.time() - started
            answer = " ".join((reply.text if reply else "").split())[:110]
            print(f"{row['route']:6} {took:6.1f} с  {row['text'][:60]:60}  {answer}", flush=True)
    finally:
        app.close()


def main() -> int:
    from jarvis import console_utf8
    console_utf8()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--live", action="store_true", help="настоящие ответы Ollama и Claude с замером времени")
    parser.add_argument("--only", choices=["local", "claude"], help="с --live: только этот маршрут")
    parser.add_argument("--limit", type=int, default=0, help="с --live: не больше N фраз")
    parser.add_argument("--model", default="", help="с --live: другая модель Ollama (по умолчанию из config.toml)")
    args = parser.parse_args()

    rows = route_cases()
    wrong = [r for r in rows if r["route"] != r["expected"]]
    width = max(len(r["text"]) for r in rows)
    for r in rows:
        mark = "ok " if r["route"] == r["expected"] else "XX "
        extra = r["detail"] or (f"навык {r['skill']}" if r["skill"] else "")
        print(f"{mark}{r['route']:6} (ждали {r['expected']:6}) {r['text']:{width}}  {extra}")
    print()
    for kind in ("fast", "local", "claude"):
        group = [r for r in rows if r["expected"] == kind]
        good = sum(r["route"] == kind for r in group)
        print(f"{kind:6}: {good}/{len(group)} верно")
    print(f"Итого: {len(rows) - len(wrong)}/{len(rows)}; ошибок маршрута: {len(wrong)}")
    if args.live:
        print("\n=== Настоящие ответы (Ollama и Claude) ===")
        live(rows, args.limit, args.only or "", args.model)
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
