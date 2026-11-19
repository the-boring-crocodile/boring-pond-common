"""Plain admission validators shared by configuration and runtime boundaries."""
from __future__ import annotations

from typing import Any, cast

# Every job has this ceiling; schema annotations live separately in inputs.
MAX_JOB_SECONDS = 86400.0


def table(value: object, name: str, required: set[str] | None = None,
          optional: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ValueError(f"{name} must be a table")
    if required is not None:
        missing = required - value.keys()
        unknown = value.keys() - required - (optional or set())
        if missing or unknown:
            raise ValueError(f"{name}: missing keys {sorted(missing)}; unknown keys {sorted(unknown)}")
    return cast(dict[str, Any], value)


def text(value: object, name: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()) or "\x00" in value:
        raise ValueError(f"{name} must be {'a' if empty else 'a nonempty'} string without NUL")
    return value


def integer(value: object, name: str, minimum: int, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{name} must be an integer >= {minimum}" +
                         (f" and <= {maximum}" if maximum is not None else ""))
    return value


def strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} must be a nonempty list of strings")
    return tuple(text(item, name) for item in value)
