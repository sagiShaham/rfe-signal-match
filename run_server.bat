@echo off
REM ─────────────────────────────────────────────────────────────
REM  Auto-start launcher for RFE Signal Match.
REM  Keeps the uvicorn server running on port 3000 and relaunches
REM  it automatically if it ever exits/crashes. Logs to server.log.
REM ─────────────────────────────────────────────────────────────
cd /d "C:\RFE Signal Platform"
:loop
echo [%date% %time%] starting server >> "server.log"
"venv\Scripts\python.exe" -m uvicorn main:app --host 0.0.0.0 --port 3000 >> "server.log" 2>&1
echo [%date% %time%] server exited, restarting in 5s >> "server.log"
timeout /t 5 /nobreak >nul
goto loop
