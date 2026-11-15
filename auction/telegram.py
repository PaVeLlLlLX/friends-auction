"""Small Telegram Bot API client. Never log token-bearing URLs or raw responses."""

import json
import urllib.error
import urllib.request

from auction.i18n import translate


class TelegramError(Exception):
    """Sanitized API failure, with delivery retry guidance only."""

    def __init__(self, message, *, retryable=True):
        super().__init__(message)
        self.retryable = retryable


def call(token, method, payload=None, timeout=15):
    body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        "https://api.telegram.org/bot" + token + "/" + method,
        body,
        {"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        retryable = error.code == 429 or error.code >= 500
        error.close()
        raise TelegramError(translate("error_telegram_rejected"), retryable=retryable) from None
    except (urllib.error.URLError, OSError, ValueError):
        raise TelegramError(translate("error_telegram_unavailable")) from None
    if not isinstance(result, dict) or not result.get("ok"):
        code = result.get("error_code") if isinstance(result, dict) else None
        retryable = type(code) is not int or code == 429 or code >= 500
        raise TelegramError(translate("error_telegram_rejected"), retryable=retryable)
    return result.get("result")
