"""Shared JSON value types for the HTTP and Telegram integration boundaries.

These aliases describe values produced by the standard JSON parser. Application
code must still validate the shape and meaning of data received from a client.
"""

from typing import TypeAlias

JSONValue: TypeAlias = str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]
JSONObject: TypeAlias = dict[str, JSONValue]
