#!/usr/bin/env bash
# Установка JARVIS на Linux (Kali, Debian, Ubuntu). Ничего не ставит в систему и не требует sudo:
# окружение .venv и Chromium для Playwright — внутри проекта / в кэше пользователя.
set -euo pipefail
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { echo "Нужен Python 3.11+"; exit 1; }
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
echo "Скачиваю Chromium для браузера Джарвиса (Playwright), около 170 МБ..."
if ! .venv/bin/python -m playwright install chromium; then
  echo "ОШИБКА: Chromium не скачан. Если не хватает системных библиотек, установите их сами:"
  echo "  sudo .venv/bin/python -m playwright install-deps chromium"
fi
if [ "${1:-}" = "--claude" ]; then
  .venv/bin/pip install -r requirements-claude.txt
  echo "Claude Agent SDK установлен. Войдите: claude login (или ANTHROPIC_API_KEY в .env)."
fi
[ -f .env ] || cp .env.example .env
.venv/bin/python -m jarvis --check --web || true
echo
echo "Готово. Запуск интерфейса: ./run_web.sh  (откроется http://127.0.0.1:8765)"
echo "Модель Claude (облако): ./install.sh --claude, затем Настройки → Модель в HUD."
