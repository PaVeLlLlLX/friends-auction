"""Application messages are stored as data, separate from the source code."""

import json
from functools import lru_cache
from pathlib import Path

MESSAGES_PATH = Path(__file__).resolve().parent.parent / "static" / "ru.json"


@lru_cache(maxsize=1)
def messages() -> dict[str, str]:
    """Load and cache the shared message catalog, rejecting non-string entries.

    The returned mapping is shared by the server, bot and local launchers.
    Missing files and invalid JSON propagate to the caller as setup errors.
    """
    with MESSAGES_PATH.open(encoding="utf-8") as source:
        document: object = json.load(source)
    if not isinstance(document, dict):
        raise ValueError("The message catalog must be a JSON object.")
    catalog: dict[str, str] = {}
    for key, value in document.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("Message keys and values must be strings.")
        catalog[key] = value
    return catalog


def translate(key: str, *args: object) -> str:
    """Return a catalog entry and apply positional format arguments when supplied.

    Unknown keys raise ``KeyError`` rather than concealing a missing translation.
    Arguments are formatted as text; callers remain responsible for escaping
    the result for HTML or other output contexts.
    """
    text = messages()[key]
    return text.format(*args) if args else text
