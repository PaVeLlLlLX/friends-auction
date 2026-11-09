#!/bin/bash
set -e
cd "$(dirname "$0")"
source scripts/messages.sh
if [ ! -x .venv/bin/python ]; then
  say shell_demo_required 'Run the demo launcher first to install dependencies.\n'
  exit 1
fi
if .venv/bin/python scripts/setup_testing.py; then
  exit 0
else
  AUCTION_SETUP_STATUS=$?
  say shell_setup_failed '\nSetup failed. Do not send your token in chat. Press Enter to close.\n'
  read -r
  exit "$AUCTION_SETUP_STATUS"
fi
