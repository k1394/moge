@echo off
chcp 65001 >nul
cd /d "%~dp0"
.venv\Scripts\python.exe notes\day1_practice.py
echo.
pause
