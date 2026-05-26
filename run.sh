#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
    echo "[ERROR] .venv not found. Run ./install.sh first." >&2
    exit 1
fi

echo "Starting Robomuffin Image Factory..."
echo "Open http://127.0.0.1:8765/ once you see 'app.ready' in the log."
echo "Press Ctrl+C to stop."
echo

exec .venv/bin/python -m app.main
