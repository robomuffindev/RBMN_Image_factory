@echo off
REM ============================================================================
REM  Robomuffin Image Factory — start server (Windows)
REM
REM  Usage:
REM    run.bat               -- uses FACTORY_PORT from .env (defaults to 8765)
REM    run.bat 8766          -- override the port for this run
REM ============================================================================
SETLOCAL ENABLEDELAYEDEXPANSION
cd /d "%~dp0"

if not exist .venv (
    echo [ERROR] .venv not found. Run installer.bat first.
    pause
    exit /b 1
)

REM -- 1) port priority: CLI arg > .env value > default 8765 -------------------
set "PORT="
if not "%~1"=="" set "PORT=%~1"
if "!PORT!"=="" if exist .env (
    for /f "usebackq tokens=2 delims==" %%v in (`findstr /b "FACTORY_PORT=" .env 2^>nul`) do (
        set "PORT=%%v"
    )
)
if "!PORT!"=="" set "PORT=8765"
REM Strip carriage returns / spaces that may have come from .env
for /f "tokens=* delims= " %%a in ("!PORT!") do set "PORT=%%a"
set "PORT=!PORT: =!"

REM Export it to the child python process.
set "FACTORY_PORT=!PORT!"

echo Starting Robomuffin Image Factory on port !PORT! ...
echo Open http://127.0.0.1:!PORT!/ in your browser once you see "app.ready" in the log.
echo Press Ctrl+C to stop.
echo.

.venv\Scripts\python.exe -m app.main
set "RC=%ERRORLEVEL%"

if not "!RC!"=="0" (
    echo.
    echo [server exited with code !RC!]
    echo.
    echo If you saw "address already in use" / "Errno 10048":
    echo   - A previous run.bat is still alive in the background.
    echo   - Find the PID:   netstat -ano ^| findstr LISTENING ^| findstr :!PORT!
    echo   - Kill it:        taskkill /PID ^<that_pid^> /F
    echo   - Or use a different port:   run.bat 8766
    echo.
    pause
)
ENDLOCAL
