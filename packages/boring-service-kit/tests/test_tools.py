"""The tool conventions: compact results, outcomes, lean schemas, strict arguments, the call log."""

from __future__ import annotations

import json
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from boring_service_kit.calllog import CallLog, summarize
from boring_service_kit.outcomes import Failed, Refused
from boring_service_kit.tools import argument_problems, as_mcp_tool, compact, default_state_dir, lean_schema


def test_compact_is_one_line_of_json():
    result = compact({"a": "ä", "b": [1, 2]})
    assert result.content[0].text == '{"a":"ä","b":[1,2]}' and not result.is_error
    assert compact({}, is_error=True).is_error


def test_outcomes_in_the_ponds_shape():
    assert Refused("not_found", "no such entry").as_dict() == \
        {"status": "refused", "code": "not_found", "message": "no such entry"}
    assert Failed("timeout", "ran out", batch=2).as_dict() == \
        {"status": "failed", "code": "timeout", "message": "ran out", "details": {"batch": 2}}


def test_as_mcp_tool_turns_outcomes_into_errors_and_logs(tmp_path):
    log = CallLog(tmp_path / "calls.jsonl")

    def fine(x: int) -> dict:
        return {"x": x}

    def refusing() -> dict:
        raise Refused("invalid_request", "never")

    assert json.loads(as_mcp_tool(fine, log)(x=1).content[0].text) == {"x": 1}
    refused = as_mcp_tool(refusing, log)()
    assert refused.is_error and json.loads(refused.content[0].text)["code"] == "invalid_request"
    lines = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert [(e["tool"], e["outcome"], e["code"]) for e in lines] == \
        [("fine", "succeeded", None), ("refusing", "refused", "invalid_request")]


def test_lean_schema_drops_annotation_titles_but_keeps_a_parameter_named_title():
    schema = {"title": "Args", "type": "object", "properties": {
        "title": {"title": "Title", "type": "string"},
        "note": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Note"}},
        "required": ["title"]}
    assert lean_schema(schema) == {"type": "object", "properties": {
        "title": {"type": "string"}, "note": {"type": ["string", "null"]}}, "required": ["title"]}


def test_argument_problems_is_one_line_naming_what_is_expected():
    class Args(BaseModel):
        model_config = ConfigDict(extra="forbid")
        path: str
        limit: int = 10

    try:
        Args.model_validate({"limit": "many", "bogus": 1})
    except ValidationError as error:
        text = argument_problems(error, Args)
    assert text.startswith("invalid arguments -- ") and "\n" not in text
    assert "unknown: bogus" in text and "missing: path" in text and "limit" in text
    assert text.endswith("Parameters: path, limit")


def test_the_call_log_summarises_never_copies(tmp_path):
    assert summarize("x" * 500) == {"chars": 500}
    assert summarize({"content": "secret file body"}) == {"content": {"chars": 16}}
    CallLog(None).record("t", {}, "succeeded", None, time.monotonic(), 0)  # no path: nothing, no error
    unwritable = CallLog(Path("/proc/no/such/dir/calls.jsonl"))
    unwritable.record("t", {}, "succeeded", None, time.monotonic(), 0)  # the log never breaks a tool


def test_default_state_dir_is_per_service(monkeypatch, tmp_path):
    # Windows reads LOCALAPPDATA, everything else XDG_STATE_HOME (failed on a dev VM).
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert default_state_dir("boring-knowledge") == tmp_path / "boring-knowledge"


def test_lean_schema_publishes_values_as_they_are():
    schema = {"type": "object", "properties": {"style": {"title": "Style", "type": "object",
              "default": {"title": "keep me"}, "examples": [{"title": "and me"}]}}}
    lean = lean_schema(schema)["properties"]["style"]
    assert lean == {"type": "object", "default": {"title": "keep me"}, "examples": [{"title": "and me"}]}


def test_a_failed_rotation_does_not_silence_the_log(tmp_path, monkeypatch):
    import os
    from boring_service_kit import calllog
    path = tmp_path / "calls.jsonl"
    path.write_text("x" * 20)
    monkeypatch.setattr(calllog, "ROTATE_BYTES", 10)

    def refuse(*args):  # Windows: the old log is open somewhere
        raise PermissionError("in use")

    monkeypatch.setattr(os, "replace", refuse)
    log = CallLog(path)
    for _ in range(2):
        log.record("t", {}, "succeeded", None, time.monotonic(), 0)
    assert path.read_text().count('"tool":"t"') == 2
