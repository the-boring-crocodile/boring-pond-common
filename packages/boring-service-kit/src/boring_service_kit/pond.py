"""The service side of the Pond's service contract (boring-crocodile `docs/SERVICES_AND_PIRATES.md`).

A service goes on the network one way: `boring_service_kit.http.ServiceConfig`.
Its `oauth` section names the control plane's introspection, `access` the
Ponds (subjects) it serves and their roles, its `pond` section the
registration. This module holds the parts that config is built from:

- **Calls** carry a bearer token minted by the control plane. It is checked by
  RFC 7662 introspection with this service's own credentials
  (`IntrospectionVerifier`, the base of `http.ServiceVerifier`, which adds the
  issuer, scopes and the `access` list) and accepted only when active, issued
  for this service's MCP URL (`aud`), not expired and naming a Pond (`sub`).
  Tools read the caller with `access.calling_author()`; it is never an
  argument.
- **The heartbeat**: a modern request whose `Mcp-Method` header is exactly
  `server/discover` passes without a token (the SDK rejects a header that
  does not match the body, so nothing else does); `attach_heartbeat` feeds
  its clientInfo name to the registration watchdog for that Pond.
- **Registration**: `POST /services/register` with HTTP Basic and an empty
  body, through `boring-registry`'s Watchdog, again when a Pond's worker goes
  silent.

A service calls `attach_heartbeat` in its `make_server`; `ServiceConfig`
uses the rest (a service that routes requests itself may call `wrap_app` and
`run` with the config's values).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx2
import uvicorn
from boring_registry import Watchdog
from mcp.server.auth.provider import AccessToken
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger("boring_service_kit.pond")

INTROSPECTION_CACHE_SECONDS = 60
CONTROL_PLANE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class ControlPlane:
    url: str  # e.g. http://127.0.0.1:18503, no trailing slash
    client_id: str
    client_secret: str

    def auth(self) -> httpx2.BasicAuth:
        return httpx2.BasicAuth(self.client_id, self.client_secret)


def read_secret(path: Path) -> str:
    secret = path.read_text(encoding="utf-8").strip()
    if not secret:
        raise SystemExit(f"client secret file {path} is empty")
    return secret


class IntrospectionFailed(Exception):
    """The control plane could not answer; a failure, never a bad token."""


class IntrospectionVerifier:
    """SDK TokenVerifier: introspection at the control plane, briefly cached.

    Positive answers are cached until min(exp, 60 s); negative answers are not
    cached. An unreachable or failing control plane raises IntrospectionFailed,
    which the server answers with a 500, never a 401.
    """

    def __init__(self, control_plane: ControlPlane, own_url: str,
                 clock: Callable[[], float] = time.time) -> None:
        self.control_plane = control_plane
        self.endpoint = f"{control_plane.url}/introspect"
        self.own_url = own_url
        self.clock = clock
        self._cache: dict[str, tuple[AccessToken, float]] = {}

    async def verify_token(self, token: str) -> AccessToken | None:
        now = self.clock()
        cached = self._cache.get(token)
        if cached is not None and now < cached[1]:
            return cached[0]
        try:
            async with httpx2.AsyncClient(timeout=CONTROL_PLANE_TIMEOUT_SECONDS, trust_env=False) as client:
                response = await client.post(self.endpoint, data={"token": token},
                                             auth=self.control_plane.auth())
        except httpx2.HTTPError as exc:
            log.error("token introspection failed: control plane unreachable: %s: %s", type(exc).__name__, exc)
            raise IntrospectionFailed(type(exc).__name__) from exc
        if response.status_code != 200:
            log.error("token introspection failed: HTTP %s", response.status_code)
            raise IntrospectionFailed(f"HTTP {response.status_code}")
        try:
            answer = response.json()
        except ValueError as exc:
            log.error("token introspection failed: the control plane answered non-JSON")
            raise IntrospectionFailed("non-JSON answer") from exc
        access = self.accept(token, answer, now)
        if access is not None:
            self._cache = {t: e for t, e in self._cache.items() if now < e[1]}
            assert access.expires_at is not None
            self._cache[token] = (access, min(float(access.expires_at), now + INTROSPECTION_CACHE_SECONDS))
        return access

    def accept(self, token: str, answer: Any, now: float) -> AccessToken | None:
        if not isinstance(answer, dict) or answer.get("active") is not True:
            return None
        aud, exp, sub = answer.get("aud"), answer.get("exp"), answer.get("sub")
        if not isinstance(aud, str) or aud != self.own_url:
            return None
        if not isinstance(exp, int) or isinstance(exp, bool) or exp <= now:
            return None
        if not isinstance(sub, str) or not sub:
            log.warning("introspection: active token for us without a sub; refused")
            return None
        client_id = answer.get("client_id")
        return AccessToken(token=token, client_id=client_id if isinstance(client_id, str) else "",
                           scopes=[], expires_at=exp, resource=aud, subject=sub)


def attach_heartbeat(server: Any, on_discover: Callable[[str], None] | None) -> None:
    """Feed each server/discover's clientInfo name (the Pond's worker) to the watchdog."""
    if on_discover is None:
        return

    async def discover_heartbeat(ctx: Any, call_next: Any) -> Any:
        result = await call_next(ctx)
        if ctx.method == "server/discover":
            params = ctx.session.client_params
            if params is not None:
                on_discover(params.client_info.name)
        return result

    server.middleware.append(discover_heartbeat)


def is_modern_discover(headers: list[tuple[bytes, bytes]]) -> bool:
    methods = [v for k, v in headers if k == b"mcp-method"]
    versions = [v for k, v in headers if k == b"mcp-protocol-version"]
    return (methods == [b"server/discover"] and len(versions) == 1
            and versions[0].decode("latin-1") in MODERN_PROTOCOL_VERSIONS)


class McpApp:
    """Lets exactly the modern server/discover heartbeat skip the bearer check."""

    def __init__(self, authenticated: ASGIApp, unauthenticated: ASGIApp, path: str) -> None:
        self.authenticated = authenticated
        self.unauthenticated = unauthenticated
        self.path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] == self.path and is_modern_discover(scope["headers"]):
            await self.unauthenticated(scope, receive, send)
            return
        await self.authenticated(scope, receive, send)


def wrap_app(server: Any, own_url: str, discover_bypass: bool = True,
             customize: Callable[[Any], None] | None = None) -> Any:
    """The server's stateless HTTP app at own_url's path, accepting only own_url's host:port.

    `customize(app)` may adjust the SDK's Starlette app (routes) before it is wrapped."""
    path = urlsplit(own_url).path
    sdk_app = server.streamable_http_app(
        streamable_http_path=path, stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                     allowed_hosts=[urlsplit(own_url).netloc]))
    if customize is not None:
        customize(sdk_app)
    return McpApp(sdk_app, server.session_manager.handle_request, path) if discover_bypass else sdk_app


class Registration:
    """register() for the Watchdog: the ack on 200, None otherwise; never raises."""

    def __init__(self, control_plane: ControlPlane) -> None:
        self.control_plane = control_plane
        self._last: str | None = None

    async def __call__(self) -> dict[str, Any] | None:
        ack, outcome = await self._attempt()
        if outcome != self._last:
            (log.info if ack is not None else log.warning)("MCP registration: %s", outcome)
            self._last = outcome
        return ack

    async def _attempt(self) -> tuple[dict[str, Any] | None, str]:
        try:
            async with httpx2.AsyncClient(timeout=CONTROL_PLANE_TIMEOUT_SECONDS, trust_env=False) as client:
                response = await client.post(f"{self.control_plane.url}/services/register", content=b"",
                                             auth=self.control_plane.auth())
        except httpx2.HTTPError as exc:
            return None, f"control plane unreachable ({type(exc).__name__})"
        if response.status_code != 200:
            return None, f"refused with HTTP {response.status_code}"
        try:
            ack = response.json()
        except ValueError:
            return None, "malformed ack (not JSON)"
        interval = ack.get("ping_interval_seconds") if isinstance(ack, dict) else None
        sources = ack.get("sources") if isinstance(ack, dict) else None
        if (not isinstance(interval, (int, float)) or isinstance(interval, bool) or not 0 < interval < math.inf
                or not isinstance(sources, list) or not all(isinstance(s, str) for s in sources)):
            return None, "malformed ack (needs ping_interval_seconds > 0 and sources: [str])"
        return ({"ping_interval_seconds": interval, "sources": sources},
                f"registered, sources {sorted(sources)}, interval {interval:g} s")


async def run(app: Any, watchdog: Watchdog | None, host: str, port: int) -> None:
    """Serve the app and keep the registration alive until uvicorn stops."""
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info", lifespan="on"))
    heartbeat = asyncio.create_task(watchdog.run(), name="registration-watchdog") if watchdog is not None else None
    if heartbeat is not None:
        heartbeat.add_done_callback(_report_watchdog_end)
    try:
        await server.serve()
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat


def _report_watchdog_end(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("registration watchdog stopped: %r -- no re-registration until restarted", task.exception())
