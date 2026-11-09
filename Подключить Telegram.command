#!/bin/bash
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  printf 'Сначала запустите файл «Запустить демо.command», чтобы установить зависимости.\n'
  exit 1
fi
if .venv/bin/python scripts/setup_testing.py; then
  exit 0
else
  AUCTION_SETUP_STATUS=$?
  printf '\nНастройка не завершена. Не отправляйте токен в чат. Нажмите Enter, чтобы закрыть окно.\n'
  read -r
  exit "$AUCTION_SETUP_STATUS"
fi
