@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo [ERROR] Python not found on PATH.
    echo.
    pause
    exit /b
)

echo [1/3] data safety check
python "G:\docker\tools\data_safety_check.py"

echo.
echo ---------------------------------------------
echo [2/3] private words check (files going public)
echo ---------------------------------------------
python "G:\docker\tools\check_private_words.py"

echo.
echo ---------------------------------------------
echo [3/3] UI text check (no markdown stars shown to user)
echo ---------------------------------------------
python "G:\docker\tools\check_ui_text.py"

echo.
echo ---------------------------------------------
echo  Press any key to close this window.
echo ---------------------------------------------
pause >nul
