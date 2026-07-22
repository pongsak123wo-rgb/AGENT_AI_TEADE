@echo off
REM ============================================================
REM  Trading Room AI - 24/7 launcher
REM  Starts backend (with crash auto-restart) + frontend.
REM  Double-click to run, or add to Task Scheduler "At log on"
REM  so it comes back automatically after a VPS reboot.
REM ============================================================

cd /d "%~dp0"

REM --- Frontend (static server) in its own window ---
start "TradingAI Frontend" cmd /k "cd /d "%~dp0frontend" && python -m http.server 5500"

REM --- Backend with auto-restart loop in this window ---
title TradingAI Backend (auto-restart)
cd /d "%~dp0backend"

:restart
echo.
echo [%date% %time%] Starting backend...
python -m uvicorn main:app --host 0.0.0.0 --port 8000
echo.
echo [%date% %time%] Backend exited (code %errorlevel%). Restarting in 5s...
timeout /t 5 /nobreak >nul
goto restart
