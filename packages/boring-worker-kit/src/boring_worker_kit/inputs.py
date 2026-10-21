"""Strict admission contracts; normalization follows validation, never replaces it."""
from __future__ import annotations

from typing import Annotated, Any, cast
from pydantic import Field

ExecTimeoutSeconds = Annotated[float, Field(strict=True, allow_inf_nan=False, ge=0.1)]
ReceiptLimit = Annotated[int, Field(strict=True, ge=1, le=100)]
# 0 means "look once"; the ceiling is this Pond's standalone (non-batch) cap.
# A job_wait step inside a batch has its own bound (batch_contracts).
JobWaitSeconds = Annotated[float, Field(strict=True, allow_inf_nan=False, ge=0, le=60)]
# Every job stops by this long: operations and Pirate jobs always, a command at
# the host task service's lifetime cap if that is shorter. There is no
# "unlimited".
MAX_JOB_SECONDS = 86400.0
JobTimeoutSeconds = Annotated[
    float, Field(strict=True, allow_inf_nan=False, ge=0.1, le=MAX_JOB_SECONDS)]


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
