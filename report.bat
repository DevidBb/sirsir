@echo off
chcp 65001 >nul
cd /d "%~dp0"
.venv\Scripts\python.exe -m jarvis --report
if exist data\report.txt start "" notepad data\report.txt
pause
