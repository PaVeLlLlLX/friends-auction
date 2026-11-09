#!/bin/bash
set -e
cd "$(dirname "$0")"
source scripts/messages.sh
if [ ! -x .venv/bin/python ]; then
  if [ -x /opt/homebrew/bin/python3.12 ]; then
    AUCTION_PYTHON=/opt/homebrew/bin/python3.12
  elif command -v python3.12 >/dev/null 2>&1; then
    AUCTION_PYTHON=python3.12
  elif command -v python3.11 >/dev/null 2>&1; then
    AUCTION_PYTHON=python3.11
  else
    AUCTION_PYTHON=python3
  fi
  if ! "$AUCTION_PYTHON" -c 'import sys; assert sys.version_info >= (3, 11)' 2>/dev/null; then
    say shell_python_required 'Python 3.11 or newer is required.\n'
    exit 1
  fi
  "$AUCTION_PYTHON" -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
fi
say shell_demo_open '\nOpen http://127.0.0.1:8080 in Safari. Press Ctrl+C to stop.\n\n'
exec .venv/bin/python demo.py
