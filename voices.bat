@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo Сначала запустите install.bat
    pause & exit /b 1
)
rem Прослушать голоса Silero и выбрать понравившийся.
.venv\Scripts\python.exe -m jarvis --voices
pause
