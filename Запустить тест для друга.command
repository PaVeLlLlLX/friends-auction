#!/bin/bash
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  printf 'Сначала запустите файл «Запустить демо.command», чтобы установить зависимости.\n'
  exit 1
fi
exec .venv/bin/python scripts/start_testing.py
