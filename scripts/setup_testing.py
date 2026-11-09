#!/usr/bin/env python3
"""Configure a dedicated Telegram bot on this Mac without printing its token."""
import getpass
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from auction.telegram import call, TelegramError

CONFIG = ROOT / 'data' / 'testing-config.json'


def main():
    os.umask(0o077)
    print('Подключение отдельного бота для теста. Токен останется на этом Mac и не появится в чате.')
    if CONFIG.exists():
        print('Настройки уже существуют. Для повторного подключения сначала сохраните нужные данные и удалите файл настроек вручную.')
        return 1
    token = getpass.getpass('Вставьте токен бота из BotFather (символы не отображаются): ').strip()
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]{20,}', token):
        print('Непохоже на токен бота. Проверьте его и запустите настройку заново.')
        return 1
    try:
        bot = call(token, 'getMe')
        webhook = call(token, 'getWebhookInfo')
    except TelegramError as error:
        print(str(error))
        return 1
    username = bot.get('username', '') if isinstance(bot, dict) else ''
    if not re.fullmatch(r'[A-Za-z0-9_]{5,32}', username):
        print('Не удалось получить username бота.')
        return 1
    if webhook.get('url'):
        print('У бота уже настроен webhook. Используйте отдельного бота, чтобы не нарушить его работу.')
        return 1
    print(f'\nБот: @{username}')
    print(f'Откройте https://t.me/{username} в Telegram, нажмите «Запустить» или отправьте /start.')
    input('После этого вернитесь сюда и нажмите Enter... ')
    try:
        updates = call(token, 'getUpdates', {'limit':100, 'timeout':0})
    except TelegramError as error:
        print(str(error))
        return 1
    people = {}
    for update in updates if isinstance(updates, list) else []:
        message = update.get('message') or {}
        person = message.get('from') or {}
        chat = message.get('chat') or {}
        text = message.get('text') or ''
        if chat.get('type') == 'private' and type(person.get('id')) is int and chat.get('id') == person['id'] and not person.get('is_bot') and (text == '/start' or text.startswith('/start ')):
            people[person['id']] = person
    if not people:
        print('Команда /start пока не найдена. Отправьте её боту и запустите настройку ещё раз.')
        return 1
    users = list(people.values())
    print('\nВыберите свой Telegram-аккаунт:')
    for i, person in enumerate(users, 1):
        name = ' '.join(str(person.get(k) or '') for k in ('first_name','last_name')).strip()
        label = '@' + person['username'] if person.get('username') else 'без username'
        print(f'{i}. {name} ({label}, ID {person["id"]})')
    if len(users) == 1:
        person = users[0]
    else:
        try:
            selected = int(input('Введите цифру слева от вашего имени (например, 1), а не Telegram ID: ').strip())
            if selected < 1 or selected > len(users):
                raise ValueError
        except ValueError:
            print('Некорректный выбор. Настройки не сохранены.')
            return 1
        person = users[selected-1]
    if input(f'Это ваш аккаунт {person.get("first_name", "")} (ID {person["id"]})? Напишите «да»: ').strip().lower() not in ('д','да','y','yes'):
        print('Настройки не сохранены.')
        return 1
    CONFIG.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = {'token':token, 'admin_id':person['id'], 'username':username}
    try:
        fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(data, file, ensure_ascii=False)
    except OSError:
        print('Не удалось безопасно сохранить настройки. Проверьте доступ к папке data.')
        return 1
    print('\nГотово. Теперь откройте файл «Запустить тест для друга.command».')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print('\nНастройка отменена.')
        raise SystemExit(1)
