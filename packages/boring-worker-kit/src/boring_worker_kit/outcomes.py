"""Typed normal outcomes shared by local tool callers."""
from __future__ import annotations
import re

CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")

# Our own vocabulary. Codes from a downstream component (a Pirate, a service)
# are passed through unchanged and need not appear here.
OUR_CODES: dict[str, str] = {
    "invalid_request": "the request itself is malformed or not acceptable; change it",
    "not_found": "the named thing (file, job, Pirate, service) does not exist here",
    "forbidden": "this Pond's policy does not allow it",
    "stale": "the file changed since it was read; re-read (see current_sha256) and retry",
    "busy": "a shared resource is held right now; retry later",
    "limit": "a size, count or capacity limit was reached",
    "not_admitted": "the work was not started (no capacity before the deadline); safe to retry",
    "unavailable": "the component that would answer is not running or not listening",
}


def is_code(value: object) -> bool:
    return isinstance(value, str) and CODE.fullmatch(value) is not None


class Refusal(Exception):
    """A normal outcome the caller can act on -- not a failure.

    Raised where the outcome is decided, caught once at the worker's request
    boundary and turned into data there. Nothing else becomes a refusal: any
    other exception is reported as a failure. (Phase 1 keeps this one typed
    exception as the carrier out of deep call chains; later phases return
    outcomes directly as they rewrite those chains.)
    """

    def __init__(self, code: str, message: str, **details: object) -> None:
        if not is_code(code):
            raise ValueError(f"invalid refusal code {code!r}")
        if not isinstance(message, str):
            raise TypeError("refusal message must be a string")
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def as_data(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, **self.details}

