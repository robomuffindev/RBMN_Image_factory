@echo off
REM ============================================================================
REM  Robomuffin Image Factory — updater.bat
REM
REM  Pulls the latest code from origin, refreshes the venv's pip packages, and
REM  runs the additive DB migration. Safe to run while the app is stopped;
REM  refuse to clobber a running server.
REM
REM  Designed to be called from another local app's updater workflow — drop
REM  this file path into your WP admin app's "update factory" button.
REM
REM  Exit codes:
REM    0  — updated cleanly
REM    1  — generic failure (venv missing, git failed, requirements failed)
REM    2  — server still running on the configured port; skipped
REM ============================================================================
SETLOCAL ENABLEDELAYEDEXPANSION
cd /d "%~dp0"

echo [updater] %DATE% %TIME%  starting

REM ---- 1) safety: refuse to update while run.bat is still listening --------
set "PORT="
if exist .env (
    for /f "usebackq tokens=2 delims==" %%v in (`findstr /b "FACTORY_PORT=" .env 2^>nul`) do (
        set "PORT=%%v"
    )
)
if "!PORT!"=="" set "PORT=8765"
for /f "tokens=* delims= " %%a in ("!PORT!") do set "PORT=%%a"
set "PORT=!PORT: =!"

netstat -ano | findstr LISTENING | findstr :!PORT! >nul 2>&1
if !ERRORLEVEL! == 0 (
    echo [updater] ERROR: Robomuffin appears to be running on port !PORT!.
    echo           Stop run.bat first (Ctrl+C in its window) then re-run updater.bat.
    exit /b 2
)

REM ---- 2) git pull ---------------------------------------------------------
if not exist .git (
    echo [updater] ERROR: this folder is not a git checkout. Clone with:
    echo           git clone https://github.com/robomuffinlabs/RBMN_Image_factory.git
    exit /b 1
)
echo [updater] git pull...
git pull --ff-only
if !ERRORLEVEL! NEQ 0 (
    echo [updater] git pull failed — fix conflicts then re-run.
    exit /b 1
)

REM ---- 3) refresh Python deps -----------------------------------------------
if not exist .venv\Scripts\python.exe (
    echo [updater] ERROR: .venv missing. Run installer.bat first.
    exit /b 1
)
echo [updater] pip install -r requirements.txt ...
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
if !ERRORLEVEL! NEQ 0 (
    echo [updater] pip install failed.
    exit /b 1
)

REM ---- 4) DB migration (additive — safe to run repeatedly) ------------------
echo [updater] running DB migration...
.venv\Scripts\python.exe -c "import asyncio; from app.db.migrations import run_all; asyncio.run(run_all())"
if !ERRORLEVEL! NEQ 0 (
    echo [updater] migration failed — the DB may be in a partial state.
    exit /b 1
)

echo [updater] done. Start with run.bat to relaunch.
exit /b 0
ENDLOCAL
