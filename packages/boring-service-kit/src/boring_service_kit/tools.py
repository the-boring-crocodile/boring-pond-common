"""Tool conventions every boring MCP server shares.

Compact one-line JSON results, outcomes as `isError` in the Pond's shape, lean
published schemas, strict arguments, one call-log line per call, and the
default state directory.
"""

from __future__ import annotations

import functools
import inspect
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, cast

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import ConfigDict, ValidationError

from .calllog import CallLog
from .outcomes import Outcome

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                            open_world_hint=False)
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                         open_world_hint=False)
RUNS = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                       open_world_hint=True)


def compact(data: Any, is_error: bool = False) -> CallToolResult:
    """One compact JSON text block, nothing else.

    The SDK's default would add an indented copy next to `structuredContent`;
    every result is re-sent on every later Copilot turn, so it costs twice,
    turn after turn. Refusals and failures are `isError` with the
    outcome as JSON, the Pond's shape: `status`, `code`, `message`, `details`.
    """
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=is_error)


def as_mcp_tool(fn: Callable[..., dict[str, Any]], log: CallLog) -> Callable[..., CallToolResult]:
    """Wrap a plain tool: compact result, outcomes as errors, no output schema, one log line."""
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> CallToolResult:
        started = time.monotonic()
        try:
            result = compact(fn(*args, **kwargs))
            log.record(fn.__name__, kwargs, "succeeded", None, started, len(result.content[0].text))
            return result
        except Outcome as outcome:
            result = compact(outcome.as_dict(), is_error=True)
            log.record(fn.__name__, kwargs, outcome.status, outcome.code, started, len(result.content[0].text))
            return result
        except Exception as exc:
            log.record(fn.__name__, kwargs, "error", type(exc).__name__, started, 0)
            raise
    # The SDK reads the signature; a CallToolResult return publishes no output schema.
    wrapper.__signature__ = inspect.signature(inspect.unwrap(fn), eval_str=True).replace(return_annotation=CallToolResult)  # type: ignore[attr-defined]
    return wrapper


VALUE_KEYS = {"default", "const", "examples", "enum"}


def lean_schema(node: Any) -> Any:
    """The published schema without what only costs tokens.

    Drops pydantic's `title` keys (they repeat the property names), writes an
    optional value as `"type": [t, "null"]` instead of an `anyOf`, and drops
    `"default": null`. Validation is unaffected: it uses the strict model.
    """
    if isinstance(node, list):
        return [lean_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    # `title` is dropped where it annotates a schema, never where it names a parameter:
    # inside `properties` (and `$defs`) the keys are names, and a parameter may be called title.
    # Values (default, const, examples, enum) are data, published as they are.
    node = {key: (value if key in VALUE_KEYS
                  else {name: lean_schema(sub) for name, sub in value.items()}
                  if key in ("properties", "$defs", "definitions") and isinstance(value, dict)
                  else lean_schema(value))
            for key, value in node.items() if key != "title"}
    options = node.get("anyOf")
    if isinstance(options, list) and len(options) == 2 and {"type": "null"} in options:
        other = next(option for option in options if option != {"type": "null"})
        if set(other) == {"type"} and isinstance(other["type"], str):
            del node["anyOf"]
            node["type"] = [other["type"], "null"]
    if "default" in node and node["default"] is None:
        del node["default"]
    return node


def argument_problems(error: ValidationError, model: Any) -> str:
    """One short line instead of pydantic's report: what is unknown, missing or wrong, and what is expected."""
    unknown, missing, wrong = [], [], []
    for problem in error.errors():
        name = ".".join(str(part) for part in problem.get("loc", ())) or "?"
        if problem.get("type") == "extra_forbidden":
            unknown.append(name)
        elif problem.get("type") == "missing":
            missing.append(name)
        else:
            wrong.append(f"{name} ({problem.get('msg', 'invalid')})")
    parts = [f"{label}: {', '.join(names)}" for label, names in
             (("unknown", unknown), ("missing", missing), ("invalid", wrong)) if names]
    expected = ", ".join(model.model_fields) or "none"
    return f"invalid arguments -- {'; '.join(parts)}. Parameters: {expected}"


def _check_first(tool: Any, strict: Any) -> None:
    """Validate before the SDK does, so a wrong call gets one short line (the SDK would pass
    pydantic's multi-line report with documentation links, and swallow any other message)."""
    original = tool.run

    async def run(arguments: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        try:
            strict.model_validate(tool.fn_metadata.pre_parse_json(arguments))
        except ValidationError as error:
            raise ToolError(f"Error executing tool {tool.name}: {argument_problems(error, strict)}") from None
        return await original(arguments, *args, **kwargs)

    object.__setattr__(tool, "run", run)


def refuse_unknown_arguments(mcp: MCPServer) -> None:
    """MCP 2.2.0 ignores unknown arguments; reject them instead (as the Pond does).

    Top-level arguments only: an argument typed as a pydantic model keeps that
    model's own setting (by default extra keys are dropped silently), so give such
    models `extra="forbid"` themselves. Also publishes the lean form of each
    schema (`lean_schema`).
    """
    for tool in mcp._tool_manager.list_tools():
        loose = tool.fn_metadata.arg_model
        strict = cast(type[ArgModelBase], type(loose.__name__, (loose,), {
            "__module__": loose.__module__,
            "model_config": ConfigDict(extra="forbid"),
        }))
        tool.fn_metadata.arg_model = strict
        _check_first(tool, strict)
        tool.parameters = lean_schema(strict.model_json_schema(by_alias=True))


def default_state_dir(service: str) -> Path:
    """Per-user state for one service: `%LOCALAPPDATA%\\<service>` or `$XDG_STATE_HOME/<service>`."""
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / service
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / service
