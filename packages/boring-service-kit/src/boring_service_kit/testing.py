"""Test helpers for services built with the kit: a fake control plane and an in-process client.

Real SDK app over httpx2's ASGI transport; the fake control plane is a stdlib
HTTP server in a thread that answers /introspect and /services/register.
For tests only: nothing here is secure or meant for a running service.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import http.server
import json
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2

from boring_service_kit.pond import ControlPlane

__all__ = ["CLIENT_ID", "OWN_URL", "SECRET", "VERSION", "FakeControlPlane", "RecordingWatchdog", "Service",
           "metadata_document", "mcp_headers", "mcp_meta", "sign_in"]

OWN_URL = "http://127.0.0.1:18599/mcp"
CLIENT_ID, SECRET = "kit-client", "kit-secret"
VERSION = "2026-07-28"


class FakeControlPlane:
    def __init__(self) -> None:
        self.tokens: dict[str, dict[str, Any]] = {}
        self.introspect_status = 200
        self.register: tuple[int, bytes] = (200, b"{}")
        self.requests: list[dict[str, Any]] = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                fake.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                if self.path == "/introspect":
                    token = parse_qs(body.decode())["token"][0]
                    status, payload = fake.introspect_status, json.dumps(fake.tokens.get(token, {"active": False}))
                elif self.path == "/services/register":
                    status, payload = fake.register[0], fake.register[1].decode()
                else:
                    status, payload = 404, "{}"
                data = payload.encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def introspections(self) -> int:
        return len([r for r in self.requests if r["path"] == "/introspect"])

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class RecordingWatchdog:
    def __init__(self) -> None:
        self.pings: list[str] = []

    def pinged(self, source: str = "") -> None:
        self.pings.append(source)


def mcp_meta(client: str = "tester") -> dict[str, Any]:
    return {"io.modelcontextprotocol/protocolVersion": VERSION,
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": client, "version": "1"}}


def mcp_headers(method: str, name: str | None = None, token: str | None = None) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "MCP-Protocol-Version": VERSION, "Mcp-Method": method}
    if name is not None:
        headers["Mcp-Name"] = name
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


class Service:
    """A service app with its lifespan running, an HTTP client into it and a fake control plane.

    Tokens known to the fake control plane: ``pond`` and ``meadow`` (valid),
    ``other-service`` (wrong audience), ``expired`` and ``no-sub``.
    """

    def __init__(self, app: Any = None, own_url: str = OWN_URL) -> None:
        self.cp = FakeControlPlane()
        now = int(time.time())
        good = {"active": True, "aud": OWN_URL, "exp": now + 300, "sub": "pond", "client_id": "control-plane"}
        self.cp.tokens.update({
            "pond": good, "meadow": dict(good, sub="meadow"),
            "other-service": dict(good, aud="http://127.0.0.1:18512/mcp"),
            "expired": dict(good, exp=now - 1), "no-sub": {k: v for k, v in good.items() if k != "sub"}})
        self.watchdog = RecordingWatchdog()
        self.control_plane = ControlPlane(self.cp.url, CLIENT_ID, SECRET)
        self.own_url = own_url
        self.app = app  # set by the test: the wrapped ASGI app of the service under test

    async def __aenter__(self) -> "Service":
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.task = asyncio.create_task(self.app({"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}},
                                                 self.inbox.get, self.outbox.put))
        await self.inbox.put({"type": "lifespan.startup"})
        assert (await self.outbox.get())["type"] == "lifespan.startup.complete"
        self.client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app, raise_app_exceptions=False),
                                         base_url=self.own_url.rsplit("/", 1)[0])
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.client.aclose()
        await self.inbox.put({"type": "lifespan.shutdown"})
        assert (await self.outbox.get())["type"] == "lifespan.shutdown.complete"
        await self.task
        self.cp.close()

    async def call(self, tool: str, arguments: dict[str, Any], token: str | None = "pond") -> httpx2.Response:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": tool, "arguments": arguments, "_meta": mcp_meta()}}
        return await self.client.post("/mcp", json=body, headers=mcp_headers("tools/call", tool, token))

    async def result(self, tool: str, arguments: dict[str, Any], token: str = "pond") -> tuple[bool, Any]:
        response = await self.call(tool, arguments, token)
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        text = result["content"][0]["text"]
        try:
            return bool(result.get("isError")), json.loads(text)
        except ValueError:
            return bool(result.get("isError")), text


def metadata_document(client_id: str, redirect_uri: str, name: str = "Test client") -> bytes:
    """A Client ID Metadata Document as Claude publishes one, for an issuer's `fetch`."""
    return json.dumps({"client_id": client_id, "client_name": name, "redirect_uris": [redirect_uri],
                       "token_endpoint_auth_methods_supported": ["none"]}).encode()


async def sign_in(client: httpx2.AsyncClient, resource: str, client_id: str, redirect_uri: str,
                  approval_secret: str, issuer_path: str = "") -> dict[str, Any]:
    """The whole sign-in at a server that is its own issuer: authorize, approve, token (PKCE).

    `issuer_path`: the path of the issuer URL, when it has one (behind a path-keeping proxy)."""
    verifier = base64.urlsafe_b64encode(b"test-verifier-" * 4).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    response = await client.get(issuer_path + "/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "state": "test",
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": resource, "scope": "mcp"})
    assert response.status_code == 302, response.text
    approve = urlsplit(response.headers["location"])
    approved = await client.post(approve.path + "?" + approve.query, data={"secret": approval_secret})
    assert approved.status_code == 302, approved.text
    code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
    token = await client.post(issuer_path + "/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri, "client_id": client_id,
        "code_verifier": verifier, "resource": resource})
    assert token.status_code == 200, token.text
    result: dict[str, Any] = token.json()
    return result
