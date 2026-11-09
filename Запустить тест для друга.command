#!/bin/bash
set -e
cd "$(dirname "$0")"
source scripts/messages.sh
if [ ! -x .venv/bin/python ]; then
  say shell_demo_required 'Run the demo launcher first to install dependencies.\n'
  exit 1
fi
exec .venv/bin/python scripts/start_testing.py
