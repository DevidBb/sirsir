# JARVIS — инструкции для Claude Code

Локальный голосовой ассистент на русском для **Windows 10/11** (целевая ОС — только Windows).
Железо владельца: i5-10400F, RTX 3060 12 ГБ, 16 ГБ ОЗУ. Без платных облачных API, кроме
необязательного провайдера Claude (Claude Agent SDK через вход в claude.ai).

Что сделано и как пользоваться — `README.md`. Устройство кода — `docs/ARCHITECTURE.md`.
Планы — `docs/ROADMAP.md`.

## Как работать в этом репозитории

- Общение с владельцем — по-русски, коротко и по делу. Владелец ждёт самостоятельной работы:
  на «дальше делай» — выбрать следующий шаг из ROADMAP/замечаний и сделать его целиком.
- Работаем прямо в `main`: после каждого законченного шага — тесты, коммит, `git push`.
- После пуша проверить CI: `gh run list --limit 4` (workflow «Windows» и «Windows installer»).
  Упал — `gh run view <id> --log`, починить, запушить снова.
- Не добавлять Linux-специфичных возможностей: Джарвис нужен только на Windows.

## Команды (из корня репозитория, в активированном `.venv`)

```bat
python -m pytest -q                          :: быстрые тесты, без сети
python -m pyflakes jarvis scripts tests      :: статический анализ
python scripts\windows_smoke.py              :: реальные Windows API
set JARVIS_REAL_TESTS=1 && python -m pytest tests\test_real_models.py -v   :: живые модели
python -m jarvis --text                      :: текстовый режим для отладки
python -m jarvis --web --voice               :: HUD + голос (то же, что run_web.bat)
python -m jarvis --report                    :: отчёт в data\report.txt
update.bat                                   :: обновление: config.toml → config.local.toml, pull, пакеты
```

## На ПК владельца можно и нужно проверять вживую

Здесь есть микрофон, GPU, Ollama и настоящая Windows. Перед тем как считать звук, распознавание,
LLM, HUD или браузер рабочими, проверь их по-настоящему (`--check`, `--report`,
`scripts\benchmark_speech.py`, `scripts\benchmark_llm.py`), а не только тестами.

## Подводные камни

- Личные настройки владельца — в `config.local.toml` (не в git). `config.toml` в репозитории не
  менять под конкретный ПК. Тесты грузят `config.toml` явным путём — личный файл к ним не применяется.
- `run_web.bat` запускает `jarvis.supervisor`: код выхода 75 — «обновить и перезапустить» из HUD.
  В `.bat`, которые может заменить обновление, весь запуск держать в одном блоке `( ... exit /b )`.
- Тесты на Windows: `write_text` превращает `\r\n` в `\r\r\n`; пути с `\`.
- Консоль cp1252: в .bat стоит `PYTHONUTF8=1`, в коде — `jarvis.console_utf8()`.
- Claude-провайдер — постоянный `ClaudeSDKClient` в своём asyncio-таске в потоке «claude-agent»
  (connect/use/close должны идти в одном таске).
- Библиотека mcp 2.x отличается от 1.x по API — в `jarvis/mcp_client.py` используются фолбэки через getattr.
- Headless-DuckDuckGo показывает капчу, поэтому браузер запускается с окном (headed).
