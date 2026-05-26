#!/usr/bin/env bash
# ===========================================================================
#  Robomuffin Image Factory — first-time installer (macOS / Linux)
# ===========================================================================
set -euo pipefail
cd "$(dirname "$0")"

echo
echo "=== Robomuffin Image Factory installer ==="
echo

# ------------------------------------------------------------------ python
PYCMD=""
for candidate in python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYCMD="$candidate"
        break
    fi
done
if [[ -z "$PYCMD" ]]; then
    echo "[ERROR] Could not find Python 3.11+. Install one and re-run." >&2
    exit 1
fi
echo "[1/6] Using Python: $PYCMD ($($PYCMD --version))"

# ------------------------------------------------------------------ venv
if [[ ! -d .venv ]]; then
    echo "[2/6] Creating virtual environment in .venv ..."
    "$PYCMD" -m venv .venv
else
    echo "[2/6] .venv already exists, reusing."
fi

# ------------------------------------------------------------------ pip
echo "[3/6] Upgrading pip ..."
.venv/bin/python -m pip install --upgrade pip --disable-pip-version-check >/dev/null

# ------------------------------------------------------------------ requirements
echo "[4/6] Installing requirements ..."
.venv/bin/python -m pip install -r requirements.txt --disable-pip-version-check

# ------------------------------------------------------------------ .env
if [[ ! -f .env ]]; then
    echo "[5/6] Creating .env from .env.example ..."
    cp .env.example .env
else
    echo "[5/6] .env already present, leaving it alone."
fi

# ------------------------------------------------------------------ migrations
echo "[6/6] Initializing database and seeding settings ..."
.venv/bin/python -m app.db.migrations

echo
echo "=== Install complete ==="
echo "Start the server with: ./run.sh"
echo "Then open: http://127.0.0.1:8765/"
echo
