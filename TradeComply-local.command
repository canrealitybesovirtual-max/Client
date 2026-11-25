#!/bin/bash
# TradeComply CA, local mode (macOS). Double-click in Finder. First run creates .venv and installs requirements;
# then it starts the app on 127.0.0.1 and opens your browser. Data is kept in ~/.tradecomply.
# If macOS blocks it: right-click > Open, or run `chmod +x TradeComply-local.command` once.
cd "$(dirname "$0")" || exit 1
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1 || ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "Python 3.11 or newer is required. Install it from https://www.python.org/downloads/macos/ and run this again."
  read -r -p "Press Return to close." _
  exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up a Python environment in .venv ..."
  "$PY" -m venv .venv || { read -r -p "Setup failed. Press Return to close." _; exit 1; }
fi
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
  echo "Installing requirements (needs internet once) ..."
  .venv/bin/python -m pip install --quiet --upgrade pip
  if ! .venv/bin/python -m pip install --quiet -r requirements.txt; then
    read -r -p "Installing requirements failed. Check your internet connection. Press Return to close." _
    exit 1
  fi
  touch .venv/.installed
fi
exec .venv/bin/python -m app.local "$@"
