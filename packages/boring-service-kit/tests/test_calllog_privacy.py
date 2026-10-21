"""Argument metadata must not turn diagnostics into a secret/content store."""
import json
import time
from boring_service_kit.calllog import CallLog


def test_free_text_and_nested_user_fields_are_not_retained(tmp_path):
    path = tmp_path / "calls.jsonl"
    arguments = {"command": "SYNTHETIC_COMMAND", "sql": "SYNTHETIC_SQL",
                 "body": "SYNTHETIC_NOTEBOOK", "evidence": [{"text": "SYNTHETIC_EVIDENCE"}],
                 "parameters": {"SYNTHETIC_KEY": "SYNTHETIC_VALUE"},
                 "SYNTHETIC_ARGUMENT": 12345, "timeout_seconds": 5}
    CallLog(path).record("fixture", arguments, "succeeded", None, time.monotonic(), 20)
    raw = path.read_text()
    assert "SYNTHETIC_" not in raw
    entry = json.loads(raw)
    assert entry["args"]["timeout_seconds"] == 5
    assert entry["args"]["command"] == {"chars": len(arguments["command"])}
    assert entry["result_bytes"] == 20
