#!/usr/bin/env bash
# Интерфейс HUD: http://127.0.0.1:8765 (доступен только с этого компьютера).
cd "$(dirname "$0")"
exec .venv/bin/python -m jarvis --web "$@"
