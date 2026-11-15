"""Small Telegram Bot API client. Never log token-bearing URLs or raw responses."""

import json
import urllib.error
import urllib.request
from typing import Protocol, cast

from auction.contracts import JSONObject, JSONValue
from auction.i18n import translate


class TelegramError(Exception):
    """Sanitized API failure, with delivery retry guidance only."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        """Record an operator-safe failure and whether another attempt may help."""
        super().__init__(message)
        self.retryable = retryable


class TelegramCaller(Protocol):
    """Callable subset needed by the update poller and its offline test doubles."""

    def __call__(self, token: str, method: str, payload: JSONObject) -> JSONValue:
        """Call a Bot API method, returning JSON or raising ``TelegramError``."""
        ...


def call(
    token: str, method: str, payload: JSONObject | None = None, timeout: float = 15
) -> JSONValue:
    """POST JSON to one Telegram Bot API method and return its ``result`` value.

    Network errors, malformed responses and API rejections become sanitized
    ``TelegramError`` instances. HTTP 429 and server failures remain retryable;
    tokens and raw responses are never included in the exception message.
    """
    body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        "https://api.telegram.org/bot" + token + "/" + method,
        body,
        {"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            # The standard JSON decoder produces only the recursive JSON value types.
            result = cast(JSONValue, json.load(response))
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
