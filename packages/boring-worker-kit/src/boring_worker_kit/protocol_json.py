"""Strict JSON objects for local protocol boundaries."""
from __future__ import annotations

import json
import math
from typing import Any, NoReturn


def _constant(value: str) -> NoReturn:
    raise ValueError("nonfinite JSON number")


def _float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite JSON number")
    return number


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def decode_object(payload: str | bytes) -> dict[str, Any]:
    value = json.loads(payload, parse_constant=_constant, parse_float=_float,
                       object_pairs_hook=_object)
    if not isinstance(value, dict):
        raise ValueError("protocol message must contain an object")
    return value

