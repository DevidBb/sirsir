"""Проверка языковой модели: выбирает ли она правильные инструменты.

Быстрые команды отключены — каждую просьбу разбирает LLM. Действия не
выполняются по-настоящему (приложения не запускаются, файлы не трогаются).

    python scripts/benchmark_llm.py                 # модель из config.toml
    python scripts/benchmark_llm.py --model qwen3:1.7b
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.config import Config  # noqa: E402
from jarvis.llm import LLMError  # noqa: E402
from jarvis.main import App  # noqa: E402

# (просьба, какой инструмент ожидается; None — ответ без инструментов)
CASES = [
    ("Запусти, пожалуйста, калькулятор", "open_app"),
    ("Мне нужен ютуб", "open_website"),
    ("Найди у меня файлы со словом отчёт", "search_files"),
    ("Сколько процентов занята оперативная память?", "system_info"),
    ("Сколько будет 37 умножить на 43?", "calculate"),
    ("Напомни мне через полчаса проверить духовку", "set_reminder"),
    ("Поставь таймер на три минуты для чая", "set_timer"),
    ("Запомни, что я люблю зелёный чай", "remember_fact"),
    ("Запиши в заметки идею: сделать бота для дома", "add_note"),
    ("Сделай звук чуть громче", "media_control"),
    ("Сверни окно браузера", "window_action"),
    ("Удали файл C:/Users/me/Documents/old.txt", "delete_file"),
    ("Выключи комп", "shutdown_computer"),
    ("Кто написал «Войну и мир»?", None),
    ("Как тебя зовут?", None),
    ("Выполни в PowerShell команду Remove-Item C:/ -Recurse", None),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", help="модель Ollama (по умолчанию из config.toml)")
    args = parser.parse_args()
    config = Config.load()
    if args.model:
        config.set("llm.model", args.model)
    config.set("memory.db_path", str(Path(tempfile.mkdtemp()) / "bench.db"))

    app = App(config, voice=False)
    try:
        if not app.llm.has_model():
            print(f"Модель {app.llm.model} не установлена: ollama pull {app.llm.model}")
            return 1
    except LLMError as e:
        print(e)
        return 1

    chosen: list[str] = []  # все инструменты, которые выбрала модель
    original_validate = app.registry.validate

    def recording_validate(name, args):
        chosen.append(name)
        return original_validate(name, args)

    app.registry.validate = recording_validate
    for name in app.registry.names():  # действия не выполняются по-настоящему
        app.registry.get(name).handler = (lambda n: lambda **kw: f"Готово ({n}).")(name)
    app.assistant.intents.match = lambda text: None  # всё решает LLM

    print(f"Модель: {app.llm.model}, инструментов: {len(app.registry.names())}\n")
    ok = 0
    times = []
    for text, expected in CASES:
        chosen.clear()
        app.assistant.conversation.clear()
        app.assistant.confirmations.cancel()
        app.assistant.guard._last_command = None
        t = time.monotonic()
        reply = app.assistant.handle(text)
        dt = time.monotonic() - t
        times.append(dt)
        pending = app.assistant.confirmations.has_pending()
        good = (not chosen) if expected is None else (expected in chosen)
        ok += good
        stats = app.llm.last_stats
        print(f"{'✓' if good else '✗'} {dt:5.1f} с  токенов запроса={stats.get('prompt_eval_count')}  {text}\n"
              f"      → инструменты: {chosen or '-'}{'  [ждёт подтверждения]' if pending else ''}\n"
              f"      → ответ: {reply.text if reply else None}")
    print(f"\nВерно: {ok}/{len(CASES)}, среднее время: {sum(times) / len(times):.1f} с")
    app.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
