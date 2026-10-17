@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
rem Интерфейс HUD (http://127.0.0.1:8765, только этот компьютер) и локальный голос.
rem Без голоса: run_web.bat --no-voice  (или python -m jarvis --web)
rem jarvis.supervisor перезапускает Джарвиса после кнопки «Обновить и перезапустить» в HUD.
rem С --tray (так запускает автозапуск) окно после выхода не ждёт нажатия клавиши.
set NOPAUSE=
echo %* | findstr /i /c:"--tray" >nul && set NOPAUSE=1
rem Всё в одном блоке: cmd читает его целиком, и обновление этого файла на ходу ему не мешает.
(
    if /i "%~1"=="--no-voice" (
        .venv\Scripts\python.exe -m jarvis.supervisor --web
    ) else (
        .venv\Scripts\python.exe -m jarvis.supervisor --web --voice %*
    )
    if not defined NOPAUSE pause
    exit /b
)
