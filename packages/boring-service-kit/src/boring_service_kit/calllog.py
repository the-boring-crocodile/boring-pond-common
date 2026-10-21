"""The call log: one JSON line per tool call.

Two jobs: measuring what Copilot actually does (calls per
task, bytes per result, which tools), and an audit trail of what ran on an
elevated machine. Argument text is never retained, including command/SQL
prefixes: only reviewed field names, lengths, counts and numeric budgets.
User-defined dictionary keys are not retained. Writing never breaks a tool.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROTATE_BYTES = 10 * 1024 * 1024
ARGUMENT_FIELDS = frozenset({
    "command", "sql", "query", "path", "root", "content", "data", "body",
    "evidence", "parameters", "edits", "operations", "new_text", "old_text",
    "timeout_seconds", "wait_seconds", "limit", "offset", "length",
    "start_line", "line_count", "tail_lines", "max_bytes", "expected_sha256",
    "name", "title", "reason", "id", "job_id", "job_ids", "warnings",
})
NUMERIC_FIELDS = frozenset({
    "timeout_seconds", "wait_seconds", "limit", "offset", "length",
    "start_line", "line_count", "tail_lines", "max_bytes",
})


def summarize(value: Any, key: str | None = None) -> Any:
    if isinstance(value, str):
        return {"chars": len(value)}
    if isinstance(value, dict):
        if key is not None:
            return {"fields": len(value)}
        result = {k: summarize(v, k) for k, v in value.items() if k in ARGUMENT_FIELDS}
        omitted = len(value) - len(result)
        if omitted:
            result["other_fields"] = omitted
        return result
    if isinstance(value, list):
        return {"edits" if key == "edits" else "items": len(value)}
    if value is None:
        return None
    if key in NUMERIC_FIELDS and type(value) in (int, float):
        return value
    return {"type": type(value).__name__}


class CallLog:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()

    def record(self, tool: str, arguments: dict[str, Any], outcome: str, code: str | None,
               started: float, result_bytes: int) -> None:
        if self.path is None:
            return
        entry = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "tool": tool,
            "outcome": outcome,  # succeeded, refused, failed, error
            "code": code,
            "ms": round((time.monotonic() - started) * 1000),
            "result_bytes": result_bytes,
            "args": summarize(arguments),
        }
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > ROTATE_BYTES:
                    os.replace(self.path, self.path.with_suffix(".1.jsonl"))
            except OSError:
                pass  # rotation failed (e.g. the old file is open on Windows): keep appending
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
            except OSError:
                pass  # the log is an observer; it never breaks a tool
