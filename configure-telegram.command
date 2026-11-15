#!/bin/bash
set -e
cd "$(dirname "$0")"
source scripts/messages.sh
ensure_environment
if .venv/bin/python scripts/setup_testing.py; then
  exit 0
else
  AUCTION_SETUP_STATUS=$?
  say shell_setup_failed '\nSetup failed. Check the message above. Press Enter to close.\n'
  read -r
  exit "$AUCTION_SETUP_STATUS"
fi
