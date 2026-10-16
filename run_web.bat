@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
rem Интерфейс HUD: http://127.0.0.1:8765 (только этот компьютер)
.venv\Scripts\python.exe -m jarvis --web %*
pause
