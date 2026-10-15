@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Установка JARVIS ===

where python >nul 2>nul
if errorlevel 1 (
    echo Python не найден. Установите Python 3.11+ с python.org, отметив "Add to PATH".
    pause & exit /b 1
)

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo Нужен Python 3.11 или новее. Рекомендуется 3.12 с python.org.
    pause & exit /b 1
)
python -c "import sys; sys.exit(0 if sys.version_info[:2] <= (3, 13) else 1)"
if errorlevel 1 (
    echo Внимание: для самой новой версии Python часть библиотек может не иметь готовых пакетов.
    echo Если установка упадёт, поставьте Python 3.12 с python.org.
)

if not exist .venv (
    echo Создаю виртуальное окружение...
    python -m venv .venv || (pause & exit /b 1)
)
call .venv\Scripts\activate.bat

echo Устанавливаю зависимости (~1.5 ГБ вместе с CUDA-библиотеками)...
python -m pip install --upgrade pip
pip install -r requirements.txt || (pause & exit /b 1)

echo.
python scripts\download_models.py || echo Модели речи не скачаны.

echo.
echo Естественный голос Silero: ~250 МБ (torch для CPU + модель 40 МБ).
set SILERO=n
set /p SILERO="Установить голос Silero? [y/N] "
if /i not "%SILERO%"=="y" goto ollama
pip install -r requirements-silero.txt --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple
python scripts\download_models.py --only-silero -y
:ollama
echo.
where ollama >nul 2>nul
if errorlevel 1 goto no_ollama
echo Модель qwen3:8b весит ~5.2 ГБ.
set PULL=n
set /p PULL="Скачать её сейчас через Ollama? [y/N] "
if /i "%PULL%"=="y" ollama pull qwen3:8b
goto checks
:no_ollama
echo Ollama не найдена. Установите с ollama.com, затем выполните: ollama pull qwen3:8b
:checks

echo.
python scripts\check_components.py
echo.
echo Готово. Запуск: run.bat
pause
