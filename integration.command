#!/bin/bash
set -e
cd "$(dirname "$0")"
source scripts/messages.sh
ensure_environment
exec .venv/bin/python scripts/start_testing.py
