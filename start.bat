@echo off
if "%1"=="" (
    echo Usage:
    echo   start bot         - Run bot with auto-restart
    echo   start backtest    - Run backtest with auto-restart
    echo   start bot --no-watch  - Run bot once
    echo   start install     - Install dependencies
    exit /b 0
)
if "%1"=="install" (
    pip install -r requirements.txt
    exit /b 0
)
python dev.py %*
