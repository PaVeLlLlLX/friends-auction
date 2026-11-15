#!/bin/bash
set -e
cd "$(dirname "$0")"
source scripts/messages.sh
ensure_environment
say shell_demo_open '\nOpen http://127.0.0.1:8080 in your browser. Press Ctrl+C to stop.\n\n'
exec .venv/bin/python demo.py
