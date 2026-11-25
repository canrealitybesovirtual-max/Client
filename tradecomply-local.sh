#!/usr/bin/env bash
# TradeComply CA, local mode (Linux). First run creates .venv and installs requirements; then it starts the app on
# 127.0.0.1 and opens your browser. Data is kept in ~/.tradecomply. Extra arguments go to `python -m app.local`.
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3.11 or newer is required. Install it with your package manager (e.g. sudo apt install python3 python3-venv)."
  exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up a Python environment in .venv ..."
  "$PY" -m venv .venv
fi
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
  echo "Installing requirements ..."
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt
  touch .venv/.installed
fi
exec .venv/bin/python -m app.local "$@"
