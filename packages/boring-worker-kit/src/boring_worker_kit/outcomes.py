"""Worker outcomes and their failure presentation; boring-outcomes owns the types."""
from __future__ import annotations

from logging import Logger
from typing import cast
from boring_outcomes import CODE, OUR_CODES, Failed, Outcome, Refused, is_code

__all__ = ["CODE", "OUR_CODES", "Failed", "Outcome", "Refused", "is_code", "failure_data"]


def failure_data(error: Exception, *, component: str, fallback_code: str,
                 logger: Logger | None) -> dict[str, object]:
    """Preserve typed outcomes; classify exceptions and keep their text in logs.

    Pass no logger only when the caller has already logged the exception.
    Imports stay here: execution and services use the outcome types themselves.
    """
    if isinstance(error, Outcome):
        return error.as_data()
    from .execution import ForegroundInterrupted
    from .process import ProcessCleanupError
    from .services import ServiceFailure
    from .task_client import ContainmentError

    if isinstance(error, ServiceFailure):
        data = error.data
        extra = {key: value for key, value in data.items() if key not in ("status", "code", "message")}
        if data.get("status") != "failed":
            raise ValueError("service failure must have failed status")
        return Failed(cast(str, data.get("code")), cast(str, data.get("message")), **extra).as_data()
    code = ("cleanup_unconfirmed" if isinstance(error, (ContainmentError, ProcessCleanupError))
            else "foreground_interrupted" if isinstance(error, ForegroundInterrupted)
            else "timeout" if isinstance(error, TimeoutError)
            else fallback_code)
    if logger is not None:
        logger.error("%s failure", component, exc_info=(type(error), error, error.__traceback__))
    return Failed(code, f"{type(error).__name__}: The {component.capitalize()} failed; see its log for details.",
                  component=component, effects="unknown").as_data()
