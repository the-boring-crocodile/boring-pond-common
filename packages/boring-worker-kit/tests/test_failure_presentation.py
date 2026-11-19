"""Shared failure policy: preserve typed outcomes, log details, return short text."""
import json
import logging

import pytest
from boring_outcomes import Failed, Refused
from boring_worker_kit.execution import ForegroundInterrupted
from boring_worker_kit.outcomes import failure_data
from boring_worker_kit.process import ProcessCleanupError
from boring_worker_kit.services import ServiceFailure
from boring_worker_kit.task_client import ContainmentError


@pytest.mark.parametrize("component", ["worker", "pirate"])
def test_unexpected_exception_is_logged_with_traceback_but_not_returned(component, caplog):
    secret = "/private/provider?token=synthetic-secret"
    logger = logging.getLogger("fixture." + component)
    with caplog.at_level(logging.ERROR, logger=logger.name):
        try:
            raise RuntimeError(secret)
        except RuntimeError as error:
            reply = failure_data(error, component=component, fallback_code=component+"_failed", logger=logger)
    assert reply == {"status": "failed", "code": component+"_failed",
                     "message": f"RuntimeError: The {component.capitalize()} failed; see its log for details.",
                     "component": component, "effects": "unknown"}
    assert secret not in json.dumps(reply)
    assert secret in caplog.text
    assert caplog.records[0].exc_info[2] is not None


@pytest.mark.parametrize("error, code", [
    (ContainmentError("private details"), "cleanup_unconfirmed"),
    (ProcessCleanupError("private details"), "cleanup_unconfirmed"),
    (ForegroundInterrupted("private details"), "foreground_interrupted"),
    (TimeoutError("private details"), "timeout"),
])
def test_known_exceptions_keep_their_cause(error, code):
    reply = failure_data(error, component="fixture", fallback_code="fixture_failed", logger=None)
    assert (reply["code"], reply["effects"]) == (code, "unknown")
    assert "private details" not in reply["message"]


@pytest.mark.parametrize("error", [
    Refused("busy", "try later", retry_after=2),
    Failed("upstream_error", "already reported", effects="unknown"),
    ServiceFailure(Failed("tool_error", "service's intended message", detail=7).as_data()),
])
def test_typed_outcomes_keep_their_message_details_and_do_not_log(error, caplog):
    expected = error.data if isinstance(error, ServiceFailure) else error.as_data()
    reply = failure_data(error, component="fixture", fallback_code="fixture_failed",
                         logger=logging.getLogger("fixture"))
    assert reply == expected
    assert caplog.records == []
