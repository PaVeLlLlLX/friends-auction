#!/bin/bash
# Launchers can report errors before the virtual environment exists.
say() {
  if [ -x .venv/bin/python ]; then
    if .venv/bin/python -c 'import sys; from auction.i18n import translate; print(translate(sys.argv[1]), end="")' "$1" 2>/dev/null; then
      return
    fi
  fi
  if command -v python3 >/dev/null 2>&1; then
    if python3 -c 'import sys; from auction.i18n import translate; print(translate(sys.argv[1]), end="")' "$1" 2>/dev/null; then
      return
    fi
  fi
  printf '%b' "$2"
}

ensure_environment() {
  if [ ! -x .venv/bin/python ]; then
    AUCTION_PYTHON=
    for candidate in /opt/homebrew/bin/python3.12 python3.14 python3.13 python3.12 python3.11 python3; do
      if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; assert sys.version_info >= (3, 11)' 2>/dev/null; then
        AUCTION_PYTHON=$candidate
        break
      fi
    done
    if [ -z "$AUCTION_PYTHON" ]; then
      say shell_python_required 'Python 3.11 or newer is required. Install it and run this launcher again.\n'
      return 1
    fi
    if ! "$AUCTION_PYTHON" -m venv .venv; then
      say shell_environment_failed 'Could not create .venv. Check Python and directory permissions.\n'
      return 1
    fi
  fi
  if ! .venv/bin/python -c 'import sys; assert sys.version_info >= (3, 11)' 2>/dev/null; then
    say shell_environment_python 'The existing .venv needs Python 3.11 or newer. Rename it and run this launcher again.\n'
    return 1
  fi
  if ! .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt; then
    say shell_dependencies_failed 'Dependency installation failed. Check the error above and rerun this launcher to retry.\n'
    return 1
  fi
}
