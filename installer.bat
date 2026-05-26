@echo off
REM ============================================================================
REM  Robomuffin Image Factory — first-time installer (Windows)
REM ============================================================================
SETLOCAL ENABLEDELAYEDEXPANSION
cd /d "%~dp0"

echo.
echo === Robomuffin Image Factory installer ===
echo.

REM ---------------------------------------------------------------- locate python
set "PYCMD="
where py >NUL 2>&1
if not errorlevel 1 (
    py -3.11 --version >NUL 2>&1
    if not errorlevel 1 ( set "PYCMD=py -3.11" ) else (
        py -3 --version >NUL 2>&1
        if not errorlevel 1 ( set "PYCMD=py -3" )
    )
)
if "!PYCMD!"=="" (
    where python >NUL 2>&1
    if not errorlevel 1 ( set "PYCMD=python" )
)
if "!PYCMD!"=="" (
    echo [ERROR] Could not find Python 3.11+. Install from https://www.python.org/downloads/ and re-run.
    pause
    exit /b 1
)
echo [1/6] Using Python: !PYCMD!
!PYCMD! --version

REM ---------------------------------------------------------------- venv
if not exist .venv (
    echo [2/6] Creating virtual environment in .venv ...
    !PYCMD! -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create venv.
        pause
        exit /b 1
    )
) else (
    echo [2/6] .venv already exists, reusing.
)

REM ---------------------------------------------------------------- pip upgrade
echo [3/6] Upgrading pip ...
.venv\Scripts\python.exe -m pip install --upgrade pip --disable-pip-version-check

REM ---------------------------------------------------------------- requirements
echo [4/6] Installing requirements ...
.venv\Scripts\python.exe -m pip install -r requirements.txt --disable-pip-version-check
if errorlevel 1 (
    echo [ERROR] Failed to install requirements.
    pause
    exit /b 1
)

REM ---------------------------------------------------------------- .env
if not exist .env (
    echo [5/6] Creating .env from .env.example ...
    copy /Y .env.example .env >NUL
) else (
    echo [5/6] .env already present, leaving it alone.
)

REM ---------------------------------------------------------------- migrations
echo [6/6] Initializing database and seeding settings ...
.venv\Scripts\python.exe -m app.db.migrations
if errorlevel 1 (
    echo [ERROR] Database initialization failed.
    pause
    exit /b 1
)

echo.
echo === Install complete ===
echo Start the server with: run.bat
echo Then open: http://127.0.0.1:8765/
echo.
pause
ENDLOCAL
