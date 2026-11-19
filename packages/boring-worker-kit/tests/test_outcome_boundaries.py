import asyncio
import json

import mcp_types as types
import pytest

from boring_outcomes import Failed, Outcome, Refused
from boring_worker_kit import outcomes
from boring_worker_kit.services import ServiceFailure, Services


def test_worker_refusal_is_the_shared_class():
    assert outcomes.Refused is Refused
    assert not hasattr(outcomes, "Refusal")
    assert isinstance(outcomes.Refused("busy", "occupied"), Outcome)


@pytest.mark.parametrize("carrier", ["structured", "text", "sdk_text"])
@pytest.mark.parametrize("status", ["refused", "failed"])
def test_service_call_preserves_valid_outcome(carrier, status):
    data = {"status": status, "code": "vendor_problem", "message": "explanation",
            "retry_after_seconds": 7, "details": {"legacy": [1, None]},
            "effects": "unknown"}
    result = types.CallToolResult(
        content=[types.TextContent(type="text", text=(
            "Error executing tool fixture: " if carrier == "sdk_text" else "") + json.dumps(data))],
        structured_content=data if carrier == "structured" else None, is_error=True)
    async def scenario():
        services = Services("fixture")
        services._available = lambda name: object()
        async def reply(*args, **kwargs):
            return result
        services._call = reply
        try:
            with pytest.raises(Refused if status == "refused" else ServiceFailure) as caught:
                await services.call("fixture", "tool", {}, "token")
            actual = caught.value.as_data() if status == "refused" else caught.value.data
            assert actual == data
        finally:
            await services.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("data", [
    {"code": "busy", "message": "missing status"},
    {"status": "succeeded", "code": "busy", "message": "wrong status"},
    {"status": "refused", "code": "BAD", "message": "bad code"},
    {"status": "failed", "code": "bad_reply", "message": 1},
    {"status": "refused", "message": "missing code"},
    {"status": "refused", "code": "busy", "message": "bad details", "number": float("nan")},
    None,
])
def test_invalid_or_unstructured_service_error_has_unknown_effects(data):
    result = types.CallToolResult(content=[types.TextContent(type="text", text="service text")],
                                  structured_content=data, is_error=True)
    async def scenario():
        services = Services("fixture")
        services._available = lambda name: object()
        async def reply(*args, **kwargs):
            return result
        services._call = reply
        try:
            with pytest.raises(ServiceFailure) as caught:
                await services.call("fixture", "tool", {}, "token")
            assert caught.value.data == Failed("upstream_error", "service text", effects="unknown").as_data()
        finally:
            await services.close()
    asyncio.run(scenario())


def test_unexpected_service_exception_is_not_reclassified():
    async def scenario():
        services = Services("fixture")
        services._available = lambda name: object()
        async def crash(*args, **kwargs):
            raise RuntimeError("fixture crash")
        services._call = crash
        try:
            with pytest.raises(RuntimeError, match="fixture crash"):
                await services.call("fixture", "tool", {}, "token")
        finally:
            await services.close()
    asyncio.run(scenario())


def test_service_protocol_error_preserves_vendor_code_and_effects():
    from types import SimpleNamespace
    from mcp.shared.exceptions import MCPError

    async def scenario():
        services = Services("fixture")
        async def protocol_error(*args, **kwargs):
            raise MCPError(-32042, "vendor protocol message")
        services._request = protocol_error
        try:
            with pytest.raises(ServiceFailure) as caught:
                await services._call(SimpleNamespace(details={"name": "fixture", "url": "http://fixture.invalid/mcp"}),
                                     "token", "tools/call", lambda client: None)
            assert caught.value.data == Failed("upstream_error", "vendor protocol message",
                                              service_code=-32042, effects="unknown").as_data()
        finally:
            await services.close()
    asyncio.run(scenario())
