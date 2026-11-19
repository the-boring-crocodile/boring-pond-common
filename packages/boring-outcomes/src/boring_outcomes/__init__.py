"""Normal outcomes: cause codes are independent of whether work started."""
from __future__ import annotations

import json
import re
from typing import ClassVar

CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")

# Closed shared causes. Source tests admit only these and documented component extras.
# Wire validation stays open so received component outcomes pass through unchanged.
OUR_CODES: dict[str, str] = {
    "invalid_request": "the request is malformed or unacceptable; change it",
    "not_found": "the named thing does not exist in this scope",
    "forbidden": "policy or permissions do not allow the operation",
    "stale": "the expected version or identity is no longer current",
    "busy": "a shared resource is occupied; retry later",
    "locked": "a lock prevents access to the resource",
    "limit": "a size, count or capacity limit was reached",
    "not_admitted": "the work could not be admitted before its deadline",
    "unavailable": "the component needed to answer is unavailable",
    "timeout": "the operation did not complete within its time limit",
    "unsupported": "the requested capability or format is not supported",
    "unreadable": "the source cannot be read or decoded",
    "conflict": "the requested change conflicts with current state",
    "bad_reply": "the downstream reply does not meet its contract",
    "cleanup_unconfirmed": "cleanup could not be confirmed",
    "upstream_error": "a downstream component reported an error",
}


def is_code(value: object) -> bool:
    return isinstance(value, str) and CODE.fullmatch(value) is not None


def _details(details: dict[str, object]) -> dict[str, object]:
    if any(not isinstance(key, str) for key in details):
        raise TypeError("outcome detail keys must be strings")
    if any(key in details for key in ("status", "code", "message")):
        raise ValueError("outcome details cannot replace status, code or message")
    # The contract uses Python's JSON acceptance, including tuples as arrays.
    # Snapshot it so later input or result edits cannot change this outcome.
    return json.loads(json.dumps(details, allow_nan=False))


class Outcome(Exception):
    """Base carrier. Render only through as_data(), with flat JSON details.

    Refused means nothing started or changed. Failed means work started and
    may have had effects. Neither status guesses the cause: both use codes.
    """
    status: ClassVar[str] = ""

    def __init__(self, code: str, message: str, **details: object) -> None:
        if self.status not in ("refused", "failed"):
            raise TypeError("use Refused or Failed")
        if not is_code(code):
            raise ValueError(f"invalid outcome code {code!r}")
        if not isinstance(message, str):
            raise TypeError("outcome message must be a string")
        self.code = code
        self.message = message
        self.details = _details(details)
        super().__init__(message)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def as_data(self) -> dict[str, object]:
        return {"status": self.status, "code": self.code,
                "message": self.message, **_details(self.details)}


class Refused(Outcome):
    """Nothing started or changed."""
    status = "refused"


class Failed(Outcome):
    """Work started and may have had effects."""
    status = "failed"
