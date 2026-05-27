@echo off
chcp 65001 > nul
title Mail Statement Parser Console
echo =========================================
echo      Starting Mail Statement Parser...
echo =========================================
python "%~dp0mail_client.py" menu
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Failed to execute mail_client.py
    echo Please make sure Python 3.11+ is installed and added to your system PATH.
    echo.
    pause
)
