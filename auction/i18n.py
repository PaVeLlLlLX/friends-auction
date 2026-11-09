"""Application messages are stored as data, separate from the source code."""
import json
from functools import lru_cache
from pathlib import Path

MESSAGES_PATH = Path(__file__).resolve().parent.parent / 'static' / 'ru.json'


@lru_cache(maxsize=1)
def messages():
    with MESSAGES_PATH.open(encoding='utf-8') as source:
        return json.load(source)


def translate(key, *args):
    text = messages()[key]
    return text.format(*args) if args else text
