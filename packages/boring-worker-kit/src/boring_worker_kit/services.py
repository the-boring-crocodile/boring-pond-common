"""Services: MCP servers the admin approved, reached by the worker as an MCP client.

The admin fixes a service's name, URL and Pond grant (`boring-crocodile-admin
service-add`). The service registers with the control plane over HTTP, which
forwards `{name, url}` to the worker of every granted Pond. From then on the
registry pings the service with MCP `server/discover` (no token; `_meta`
clientInfo name = this Pond) and calls reach it as stateless Streamable HTTP
requests that carry a bearer token the control plane minted for this Pond and
this service. The agent's own token never leaves the control plane.

Outcomes are data: an unknown service is `not_found`, one that missed its last
ping `unavailable`; a tool error or a JSON-RPC error from the service is a
refusal with the service's code and message. A connection that fails after the
request was sent is a failure: the request may have run, and it is not re-sent.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import sys
import time
from collections.abc import Awaitable, Callable, Generator
from typing import Any, TypeVar, cast

import httpx2
import mcp_types as types
from boring_registry import PING_TIMEOUT_SECONDS, POND_CALL_SECONDS, Entry, Registry
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types.version import LATEST_MODERN_VERSION

from .service_contract import SERVICE_NAME, service_ping_timeout, service_url
from .outcomes import Refusal, is_code


class ServiceFailure(RuntimeError):
    """A downstream failed/unknown outcome: may have effects, never a refusal."""
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        super().__init__(json.dumps(data, separators=(",", ":"), ensure_ascii=False))

if sys.version_info >= (3, 11):
    from builtins import BaseExceptionGroup
else:
    from exceptiongroup import BaseExceptionGroup

MAX_SERVICES = 32
CALL_TIMEOUT_SECONDS = POND_CALL_SECONDS
# The SDK prefixes a tool's ToolError text with this and the tool's name.
TOOL_ERROR_PREFIX = "Error executing tool "
_T = TypeVar("_T")
_CALL: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("service_call", default=None)


class _Bearer(httpx2.Auth):
    """Authorization for the call in progress, if any, and its HTTP status.

    The SDK sends each request from the context of the code that issued it, so
    a token set around one call reaches only that call's requests. Pings run
    in the registry's own tasks and carry none. The SDK reports a non-2xx
    answer without its status, so the status is kept here.
    """

    def auth_flow(self, request: httpx2.Request) -> Generator[httpx2.Request, httpx2.Response, None]:
        call = _CALL.get()
        if call is None:
            yield request
            return
        request.headers["Authorization"] = f"Bearer {call['token']}"
        call["status"] = (yield request).status_code


def _cause(error: BaseException) -> BaseException:
    """The one exception inside the SDK's task-group wrapping."""
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    return error


async def _never_pinged() -> bool:
    raise RuntimeError("a transient granted entry is not part of the heartbeat")


def _tool_refusal(result: types.CallToolResult) -> Refusal | ServiceFailure:
    """Classify a tool error without erasing failed/possibly-affected outcomes.

    The service states its code and message as structured content
    `{code, message, ...}`, or as that JSON object for the text of a ToolError.
    Any other tool error is `tool_error` with the service's text.
    """
    data: object = result.structured_content
    text = " ".join(item.text for item in result.content if isinstance(item, types.TextContent))
    if not isinstance(data, dict):
        body = text.partition(": ")[2] if text.startswith(TOOL_ERROR_PREFIX) else text
        try:
            data = json.loads(body)
        except ValueError:
            data = None
    if isinstance(data, dict) and is_code(data.get("code")) and isinstance(data.get("message"), str):
        if data.get("status") not in (None, "refused"):
            return ServiceFailure(data)
        extra = {key: value for key, value in data.items() if key not in ("code", "message")}
        return Refusal(data["code"], data["message"], **extra)
    return ServiceFailure({"status": "failed", "code": "tool_error",
                           "message": text or "service tool error", "effects": "unknown"})


class Services:
    """Registered services of one Pond, kept alive by the registry's heartbeat."""

    def __init__(self, pond: str, registry: Registry | None = None) -> None:
        self.pond = pond
        self.registry = registry or Registry()
        # One HTTP client (one connection pool) for every service request.
        self.http = httpx2.AsyncClient(auth=_Bearer(), timeout=httpx2.Timeout(CALL_TIMEOUT_SECONDS),
                                      trust_env=False)

    async def _request(self, url: str, call: dict[str, Any] | None,
                       send: Callable[[Client], Awaitable[_T]]) -> _T:
        """One stateless MCP request: no session, nothing to keep."""
        reset = _CALL.set(call)
        try:
            async with Client(streamable_http_client(url, http_client=self.http,
                                                     terminate_on_close=False),
                              mode=LATEST_MODERN_VERSION, cache=None,
                              client_info=types.Implementation(name=self.pond, version="")) as client:
                return await send(client)
        except BaseExceptionGroup as group:
            cause = _cause(group)
            if isinstance(cause, Exception):
                raise cause from None
            raise
        finally:
            _CALL.reset(reset)

    def register(self, request: dict[str, object]) -> dict[str, Any]:
        """Forwarded by the control plane, which authenticated the service."""
        name, url = request.get("name"), request.get("url")
        if not isinstance(name, str) or not SERVICE_NAME.fullmatch(name) or not isinstance(url, str):
            raise Refusal("invalid_request", "a service registers with its name and url")
        try:
            service_url(url)
        except ValueError as error:
            raise Refusal("invalid_request", str(error)) from None
        timeout = request.get("ping_timeout_seconds", PING_TIMEOUT_SECONDS)
        try:
            service_ping_timeout(timeout)
        except ValueError as error:
            raise Refusal("invalid_request", str(error)) from None
        identity = f"service/{name}"
        if self.registry.get(identity) is None and len(self.registry.list()) >= MAX_SERVICES:
            raise Refusal("limit", "the service registry is full")

        async def ping() -> bool:
            await self._request(url, None, lambda client: client.session.send_discover(
                LATEST_MODERN_VERSION))
            return True

        return self.registry.register(identity, ping, details={"name": name, "url": url},
                                      timeout=cast(float, timeout))

    def forget(self, name: object) -> dict[str, object]:
        """The control plane withdrew this Pond's grant: stop pinging and listing it."""
        if not isinstance(name, str) or not SERVICE_NAME.fullmatch(name):
            raise Refusal("invalid_request", "service must be a name")
        return {"forgotten": self.registry.remove(f"service/{name}")}

    def list(self) -> dict[str, object]:
        return {"services": [{
            "name": entry.details["name"], "available": entry.available,
            "misses": entry.misses, "last_miss": entry.last_miss,
        } for entry in self.registry.list()]}

    def _available(self, name: object) -> Entry:
        if not isinstance(name, str):
            raise Refusal("invalid_request", "service must be a name")
        entry = self.registry.get(f"service/{name}")
        if entry is None:
            raise Refusal("not_found", "no such service is registered with this Pond")
        if not entry.available:
            raise Refusal("unavailable", f"{name} missed its last ping: {entry.last_miss}")
        return entry

    async def _call(self, entry: Entry, token: object, method: str,
                    send: Callable[[Client], Awaitable[_T]], timeout: float | None = None) -> _T:
        if not isinstance(token, str) or not token:
            raise Refusal("invalid_request", "a service request needs the control plane's token")
        name, url = entry.details["name"], entry.details["url"]
        # The caller's time left (a carried deadline), never more than the service contract.
        limit = CALL_TIMEOUT_SECONDS if timeout is None else min(CALL_TIMEOUT_SECONDS, timeout)
        if limit <= 0:
            raise Refusal("limit", f"no time left for {method} to {name}; nothing was sent")
        call: dict[str, Any] = {"token": token}
        try:
            return await asyncio.wait_for(self._request(url, call, send), limit)
        except httpx2.ConnectError:
            raise Refusal("unavailable", f"{name} accepts no connection at {url}; "
                          "nothing was sent") from None
        except MCPError as error:
            if call.get("status") in (401, 403):
                raise Refusal("forbidden", "the service rejected the call token") from None
            raise ServiceFailure({"status": "failed", "code": "service_error", "message": error.message,
                                  "service_code": error.code, "effects": "unknown"}) from None
        except asyncio.TimeoutError:
            raise TimeoutError(f"{name} did not answer {method} within {limit:g} s; "
                               "the request may have run") from None
        except httpx2.TransportError as error:
            raise ConnectionError(f"the connection to {name} failed during {method} "
                                  f"({type(error).__name__}: {error}); the request may have run") from None

    def _granted(self, name: object, grant: dict[str, object]) -> Entry:
        """A fresh authority grant admits a direct caller without a Pond heartbeat.

        The caller must obtain this grant from its authenticated authority, never
        from model arguments. URL validation and downstream handling stay here.
        """
        if grant.get("name") != name:
            raise Refusal("forbidden", "grant names another service")
        url = grant.get("url")
        if not isinstance(name, str) or not SERVICE_NAME.fullmatch(name) or not isinstance(url, str):
            raise Refusal("invalid_request", "a grant names a service and its url")
        try:
            service_url(url)
        except ValueError as error:
            raise Refusal("invalid_request", str(error)) from None
        # A transient entry for this call only: the shared heartbeat registry
        # (its liveness, misses and capacity) stays the heartbeat's own.
        now = time.monotonic()
        return Entry(f"service/{name}", _never_pinged, {"name": name, "url": url}, now, now)

    async def describe(self, name: object, token: object, *, grant: dict[str, object] | None = None,
                       timeout: float | None = None) -> dict[str, object]:
        """The service's tools, as its MCP tools/list states them."""
        entry = self._granted(name, grant) if grant is not None else self._available(name)
        listed = await self._call(entry, token, "tools/list", lambda client: client.session.send_request(
            types.ListToolsRequest(), types.ListToolsResult), timeout)
        return {"service": name, "tools": [tool.model_dump(by_alias=True, mode="json", exclude_none=True)
                                           for tool in listed.tools]}

    async def call(self, name: object, tool: object, arguments: object, token: object, *,
                   grant: dict[str, object] | None = None, timeout: float | None = None) -> dict[str, object]:
        """One MCP tools/call; the service owns the tool's argument validation. `timeout`:
        the caller's time left, capped by CALL_TIMEOUT_SECONDS; zero or less is refused
        (`limit`) before anything is sent. A timeout or broken connection after sending
        raises (the call may have run); the service's own refusal comes back with its code."""
        entry = self._granted(name, grant) if grant is not None else self._available(name)
        if not isinstance(tool, str) or not tool:
            raise Refusal("invalid_request", "tool must be a nonempty name")
        if not isinstance(arguments, dict):
            raise Refusal("invalid_request", "arguments must be an object")
        request = types.CallToolRequest(params=types.CallToolRequestParams(name=tool, arguments=arguments))
        result = await self._call(entry, token, "tools/call", lambda client: client.session.send_request(
            request, types.CallToolResult), timeout)
        if result.is_error:
            raise _tool_refusal(result)
        answer: dict[str, object] = {"content": [item.model_dump(by_alias=True, mode="json", exclude_none=True)
                                                 for item in result.content]}
        if result.structured_content is not None:
            answer["structuredContent"] = result.structured_content
        return answer

    async def dispatch(self, request: dict[str, object]) -> dict[str, object]:
        operation = request.get("operation")
        keys = set(request) - {"request_id"}
        if operation == "service_register" and keys == {"operation", "name", "url", "ping_timeout_seconds"}:
            return self.register(request)
        if operation == "service_forget" and keys == {"operation", "name"}:
            return self.forget(request["name"])
        if operation == "service_list" and keys == {"operation"}:
            return self.list()
        if operation == "service_describe" and keys == {"operation", "service", "token"}:
            return await self.describe(request["service"], request["token"])
        if operation == "service_call" and keys == {"operation", "service", "tool", "arguments", "token"}:
            return await self.call(request["service"], request["tool"], request["arguments"], request["token"])
        raise Refusal("invalid_request", "invalid service worker envelope")

    async def close(self) -> None:
        await self.http.aclose()
