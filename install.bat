@echo off
chcp 65001 >nul
rem UTF-8 для Python: иначе русский текст падает с UnicodeEncodeError, когда вывод не в консоль.
set PYTHONUTF8=1
cd /d "%~dp0"
echo === Установка JARVIS ===

rem install.bat /auto — без вопросов (модели речи скачать, Silero и qwen3 не ставить), для автоматизации.
set AUTO=0
if /i "%~1"=="/auto" set AUTO=1
set ERRORS=0

where python >nul 2>nul
if errorlevel 1 goto no_python
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 goto old_python
python -c "import sys; sys.exit(0 if sys.version_info[:2] <= (3, 13) else 1)"
if errorlevel 1 echo Внимание: для самой новой версии Python часть библиотек может не иметь готовых пакетов. Если установка упадёт, поставьте Python 3.12 с python.org.

if exist .venv\Scripts\python.exe goto venv_ready
echo Создаю виртуальное окружение...
python -m venv .venv
if errorlevel 1 goto fail
:venv_ready
call .venv\Scripts\activate.bat

echo Устанавливаю зависимости, около 1.5 ГБ вместе с CUDA-библиотеками...
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 goto fail

echo.
if "%AUTO%"=="1" goto models_auto
python scripts\download_models.py
goto models_done
:models_auto
python scripts\download_models.py -y
:models_done
if errorlevel 1 (
    echo ОШИБКА: модели речи не скачаны - без них помощник не сможет слушать.
    echo Повторите позже: python scripts\download_models.py
    set ERRORS=1
)

echo.
if "%AUTO%"=="1" goto ollama
echo Естественный голос Silero: около 250 МБ - torch для CPU и модель 40 МБ.
set SILERO=n
set /p SILERO="Установить голос Silero? [y/N] "
if /i not "%SILERO%"=="y" goto ollama
pip install -r requirements-silero.txt --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple
if errorlevel 1 set ERRORS=1
python scripts\download_models.py --only-silero -y
if errorlevel 1 set ERRORS=1

:ollama
echo.
where ollama >nul 2>nul
if errorlevel 1 goto no_ollama
if "%AUTO%"=="1" goto checks
echo Модель qwen3:8b весит около 5.2 ГБ.
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
if "%ERRORS%"=="1" goto done_errors
echo Готово. Дальше: setup_mic.bat - настройка микрофона, затем run.bat - запуск.
if "%AUTO%"=="0" pause
exit /b 0

:done_errors
echo Установка завершена С ОШИБКАМИ - см. сообщения выше.
if "%AUTO%"=="0" pause
exit /b 1

:no_python
echo Python не найден. Установите Python 3.12 с python.org, отметив "Add to PATH".
goto fail
:old_python
echo Нужен Python 3.11 или новее. Рекомендуется 3.12 с python.org.
:fail
echo Установка прервана.
if "%AUTO%"=="0" pause
exit /b 1
