"""Small Telegram Bot API client. Never log token-bearing URLs or raw responses."""
import json
import urllib.error
import urllib.request


class TelegramError(Exception):
    pass


def call(token, method, payload=None, timeout=15):
    body = json.dumps(payload or {}, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request('https://api.telegram.org/bot' + token + '/' + method, body,
                                     {'Content-Type':'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise TelegramError('Не удалось связаться с Telegram API.') from None
    if not isinstance(result, dict) or not result.get('ok'):
        raise TelegramError('Telegram API отклонил запрос.')
    return result.get('result')
