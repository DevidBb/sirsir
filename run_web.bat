@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
rem Интерфейс HUD (http://127.0.0.1:8765, только этот компьютер) и локальный голос.
rem Без голоса: run_web.bat --no-voice  (или python -m jarvis --web)
if /i "%~1"=="--no-voice" (
    .venv\Scripts\python.exe -m jarvis --web
) else (
    .venv\Scripts\python.exe -m jarvis --web --voice %*
)
pause
