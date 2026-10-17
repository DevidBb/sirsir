@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
rem Обновление: git pull без конфликтов (правки config.toml уходят в config.local.toml) + пакеты.
if not exist .venv\Scripts\python.exe (
    echo Сначала запустите install.bat.
    pause
    exit /b 1
)
.venv\Scripts\python.exe -m jarvis.updater %*
set CODE=%ERRORLEVEL%
if /i not "%~1"=="/auto" pause
exit /b %CODE%
