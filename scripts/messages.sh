#!/bin/bash
# Launchers can report errors before the virtual environment exists.
say() {
  if [ -x .venv/bin/python ]; then
    .venv/bin/python -c 'import sys; from auction.i18n import translate; print(translate(sys.argv[1]), end="")' "$1"
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import sys; from auction.i18n import translate; print(translate(sys.argv[1]), end="")' "$1"
  else
    printf '%b' "$2"
  fi
}
