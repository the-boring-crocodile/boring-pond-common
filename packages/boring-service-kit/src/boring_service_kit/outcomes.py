"""Outcomes as data.

A step either succeeded, was refused (it did not start: nothing changed), or
failed (it started and may have had effects). Tools raise these; the MCP
layer turns them into structured results. Never a refusal after effects.
"""

from __future__ import annotations

from typing import Any


class Outcome(Exception):
    status = ""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"status": self.status, "code": self.code, "message": self.message}
        if self.details:
            result["details"] = self.details
        return result


class Refused(Outcome):
    """The step did not start. Nothing changed."""

    status = "refused"


class Failed(Outcome):
    """The step started and did not complete. It may have had effects."""

    status = "failed"
