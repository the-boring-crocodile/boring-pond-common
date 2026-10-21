"""The service side of SERVICES_AND_PIRATES.md, on the one path services use: a server
put on the network by ServiceConfig with an `oauth`, an `access` and a `pond` section.

Real SDK app over httpx2's ASGI transport; a fake control plane (a stdlib HTTP
server in a thread) answers /introspect and /services/register.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer

from boring_service_kit.access import calling_author, permitted
from boring_service_kit.calllog import CallLog
from boring_service_kit.http import ServiceConfig
from boring_service_kit.outcomes import Refused
from boring_service_kit.pond import ControlPlane, Registration, attach_heartbeat
from boring_service_kit.testing import CLIENT_ID, OWN_URL, SECRET, FakeControlPlane, mcp_headers, mcp_meta
from boring_service_kit.testing import Service as HarnessService
from boring_service_kit.tools import READ_ONLY, as_mcp_tool, refuse_unknown_arguments

# The Ponds this service serves; the harness's control plane also knows tokens for others.
ACCESS = [{"subject": "pond", "role": "writer", "author": "pond"},
          {"subject": "meadow", "role": "reader", "author": "meadow"}]


def make_server(auth: dict[str, Any], on_discover: Any) -> MCPServer:
    """What a service does with the kit: its own server, the config's auth, heartbeat and conventions."""
    server = MCPServer("kit-test", **auth)
    attach_heartbeat(server, on_discover)
    log = CallLog(None)

    def whoami() -> dict[str, Any]:
        """The calling Pond."""
        return {"pond": calling_author()}

    def refuse(reason: str) -> dict[str, Any]:
        """Always refuses."""
        raise Refused("invalid_request", reason)

    for fn in (whoami, refuse):
        server.tool(annotations=READ_ONLY)(as_mcp_tool(permitted(fn, "reader"), log))
    refuse_unknown_arguments(server)
    return server


def service_config(directory: Path, control_plane: str) -> ServiceConfig:
    """A family service's config: introspection at the Crocodile, the Ponds it serves, registration."""
    (directory / "secret").write_text(SECRET)
    path = directory / "config.json"
    path.write_text(json.dumps({
        "url": OWN_URL,
        "oauth": {"issuer": control_plane, "introspection_url": control_plane + "/introspect",
                  "client_id": CLIENT_ID, "secret_file": "secret"},
        "access": ACCESS,
        "pond": {"url": control_plane, "client_id": CLIENT_ID, "secret_file": "secret"}}))
    return ServiceConfig(path)


class Service(HarnessService):
    """The kit's own minimal server, put on the network by ServiceConfig.

    The config's watchdog is not run here (no registration loop in a unit test);
    discover pings go to the harness's recording watchdog instead."""

    def __init__(self, directory: Path, make: Callable[[dict[str, Any], Any], Any] = make_server) -> None:
        super().__init__()
        self.config = service_config(directory, self.cp.url)
        assert self.config.watchdog is not None  # a pond section: the discover heartbeat passes untokened
        self.app = self.config.app(make(self.config.auth, self.watchdog.pinged))


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_the_caller_is_the_pond_named_in_its_token(tmp_path):
    async def scenario() -> None:
        async with Service(tmp_path) as service:
            assert await service.result("whoami", {}, token="pond") == (False, {"pond": "pond"})
            assert await service.result("whoami", {}, token="meadow") == (False, {"pond": "meadow"})
    run(scenario())


def test_a_valid_token_for_a_pond_not_in_access_is_refused(tmp_path):
    async def scenario() -> None:
        async with Service(tmp_path) as service:
            service.cp.tokens["garden"] = dict(service.cp.tokens["pond"], sub="garden")
            assert (await service.call("whoami", {}, token="garden")).status_code == 401
            assert await service.result("whoami", {}, token="pond") == (False, {"pond": "pond"})
    run(scenario())


def test_tokens_that_are_not_ours_are_refused(tmp_path):
    async def scenario() -> None:
        async with Service(tmp_path) as service:
            for token in (None, "unknown", "other-service", "expired", "no-sub"):
                assert (await service.call("whoami", {}, token=token)).status_code == 401, token
    run(scenario())


def test_a_good_token_is_introspected_once_then_cached(tmp_path):
    async def scenario() -> None:
        async with Service(tmp_path) as service:
            for _ in range(3):
                assert await service.result("whoami", {}) == (False, {"pond": "pond"})
            assert service.cp.introspections() == 1
            for _ in range(2):  # refusals are never cached
                assert (await service.call("whoami", {}, token="unknown")).status_code == 401
            assert service.cp.introspections() == 3
            request = next(r for r in service.cp.requests if r["path"] == "/introspect")
            assert request["auth"] == "Basic " + base64.b64encode(f"{CLIENT_ID}:{SECRET}".encode()).decode()
    run(scenario())


def test_discover_needs_no_token_and_feeds_the_watchdog(tmp_path):
    async def scenario() -> None:
        async with Service(tmp_path) as service:
            body = {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": mcp_meta("orchard")}}
            response = await service.client.post("/mcp", json=body, headers=mcp_headers("server/discover"))
            assert response.status_code == 200, response.text
            assert service.watchdog.pings == ["orchard"]
            assert service.cp.introspections() == 0
    run(scenario())


def test_a_mismatched_method_header_does_not_bypass_auth(tmp_path):
    async def scenario() -> None:
        async with Service(tmp_path) as service:
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "whoami", "arguments": {}, "_meta": mcp_meta()}}
            response = await service.client.post("/mcp", json=body, headers=mcp_headers("server/discover"))
            assert response.status_code != 200 or "result" not in response.json()
            assert service.watchdog.pings == []
    run(scenario())


def test_only_the_own_host_is_accepted(tmp_path):
    async def scenario() -> None:
        async with Service(tmp_path) as service:
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "whoami", "arguments": {}, "_meta": mcp_meta()}}
            headers = dict(mcp_headers("tools/call", "whoami", "pond"), Host="evil.example")
            response = await service.client.post("/mcp", json=body, headers=headers)
            assert response.status_code in (400, 421), response.status_code
    run(scenario())


def test_refusals_read_as_the_worker_reads_them(tmp_path):
    async def scenario() -> None:
        async with Service(tmp_path) as service:
            is_error, refusal = await service.result("refuse", {"reason": "no"})
            # The worker's rule (services.py, _tool_refusal): JSON with a code and a message.
            assert is_error and refusal == {"status": "refused", "code": "invalid_request", "message": "no"}
    run(scenario())


def test_unknown_arguments_are_refused_in_one_line(tmp_path):
    async def scenario() -> None:
        async with Service(tmp_path) as service:
            is_error, text = await service.result("whoami", {"bogus": 1})
            assert is_error and "unknown: bogus" in text and "\n" not in text
    run(scenario())


def test_a_failing_or_unreachable_control_plane_is_a_failure_not_a_bad_token(tmp_path):
    async def scenario() -> None:
        async with Service(tmp_path) as service:
            service.cp.introspect_status = 503
            assert (await service.call("whoami", {})).status_code == 500
            service.cp.introspect_status = 200
            assert service.config.verifier is not None
            service.config.verifier.endpoint = "http://127.0.0.1:9/introspect"  # nothing listens there
            assert (await service.call("whoami", {})).status_code == 500
            assert service.watchdog.pings == []
    run(scenario())


def test_registration_ack_refusal_and_malformed():
    async def scenario() -> None:
        cp = FakeControlPlane()
        try:
            registration = Registration(ControlPlane(cp.url, CLIENT_ID, SECRET))
            cp.register = (200, b'{"ping_interval_seconds": 3, "sources": ["pond", "meadow"]}')
            assert await registration() == {"ping_interval_seconds": 3, "sources": ["pond", "meadow"]}
            request = cp.requests[0]
            assert request["path"] == "/services/register" and request["body"] == b""
            assert request["auth"] == "Basic " + base64.b64encode(f"{CLIENT_ID}:{SECRET}".encode()).decode()
            cp.register = (401, b'{"error": "invalid_client"}')
            assert await registration() is None
            for payload in (b"not json", b"[]", b'{"ping_interval_seconds": 0, "sources": []}',
                            b'{"ping_interval_seconds": true, "sources": []}',
                            b'{"ping_interval_seconds": 3, "sources": [1]}',
                            b'{"ping_interval_seconds": NaN, "sources": []}',
                            b'{"ping_interval_seconds": Infinity, "sources": []}'):
                cp.register = (200, payload)
                assert await registration() is None, payload
        finally:
            cp.close()
    run(scenario())


def test_registration_with_no_control_plane_is_none_not_an_exception():
    async def scenario() -> None:
        registration = Registration(ControlPlane("http://127.0.0.1:9", CLIENT_ID, SECRET))
        assert await registration() is None
    run(scenario())


def slow_server(seconds: float) -> Callable[[dict[str, Any], Any], Any]:
    """A server whose one tool works for a while without awaiting (a long query)."""
    def make(auth: dict[str, Any], on_discover: Any) -> MCPServer:
        server = MCPServer("kit-slow", **auth)
        attach_heartbeat(server, on_discover)

        def busy() -> dict[str, Any]:
            """Works without yielding."""
            import time
            time.sleep(seconds)
            return {"done": True}

        server.tool(annotations=READ_ONLY)(as_mcp_tool(busy, CallLog(None)))
        return server
    return make


def test_heartbeat_is_answered_while_a_tool_works(tmp_path):
    """The Pond pings every 3 s and waits 1 s; a service busy with one long call must still answer,
    or it drops out of service_list in the middle of that call."""
    import time

    async def scenario() -> None:
        async with Service(tmp_path, slow_server(1.5)) as service:
            call = asyncio.create_task(service.call("busy", {}))
            await asyncio.sleep(0.3)
            body = {"jsonrpc": "2.0", "id": 2, "method": "server/discover", "params": {"_meta": mcp_meta("orchard")}}
            started = time.monotonic()
            response = await service.client.post("/mcp", json=body, headers=mcp_headers("server/discover"))
            answered = time.monotonic() - started
            assert not call.done(), "the tool must still be working when the ping is answered"
            assert response.status_code == 200, response.text
            assert (await call).status_code == 200
            assert answered < 0.5, f"heartbeat waited {answered:.2f} s for the busy tool"
    run(scenario())
