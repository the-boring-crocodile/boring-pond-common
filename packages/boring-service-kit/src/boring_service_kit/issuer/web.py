"""The issuer's HTTP side: settings for the SDK, the approval page, the metadata it advertises."""
from __future__ import annotations

import asyncio
import re
from html import escape
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.routes import build_metadata, cors_middleware
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from .clients import AUTH_METHODS
from .provider import ConsentDetails, Denied, SQLiteIssuer

APPROVAL_BODY_BYTES = 16384
# What an issuer serves besides its metadata, at the root of its URL: a server
# that routes requests itself (beside other web pages) sends these to the SDK app.
ISSUER_PATHS = ("/authorize", "/token", "/register", "/revoke", "/approve")
CONSENT_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                               "base-uri 'none'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
_SOURCE_SAFE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:(//[A-Za-z0-9.\[\]:-]+)?")


def consent_headers(redirect_uri: str | None = None) -> dict[str, str]:
    """CONSENT_HEADERS, with the request's callback allowed as a form target.

    Chromium applies `form-action` to the redirect that follows a form post,
    so the approval page must name the callback's origin (a loopback port, a
    custom scheme) or the browser silently stays on the page.
    """
    if not redirect_uri:
        return CONSENT_HEADERS
    parts = urlsplit(redirect_uri)
    source = f"{parts.scheme}://{parts.netloc}" if parts.netloc else f"{parts.scheme}:"
    if not _SOURCE_SAFE.fullmatch(source):
        return CONSENT_HEADERS
    csp = CONSENT_HEADERS["Content-Security-Policy"].replace("form-action 'self';", f"form-action 'self' {source};")
    return {**CONSENT_HEADERS, "Content-Security-Policy": csp}


WHO = {
    "admin": "added by this server's administrator",
    "metadata": "identified by its published document on {host}",
    "dynamic": "registered itself; its name is not verified",
}
# The family's page look (Fishmarket's): inline only, the CSP allows no other source.
STYLE = ("body{margin:0;background:#f4f6f2;color:#203528;font:17px/1.5 system-ui,sans-serif}"
         "main{max-width:580px;margin:5vh auto;padding:32px;background:#fff;border:1px solid #d8e1d4;"
         "border-radius:12px}.mark{font:24px monospace;color:#476e52}h1{margin:10px 0}"
         ".to{font-size:1.5rem;font-weight:700;word-break:break-all;margin:.2rem 0 1rem}"
         ".warn{border-left:4px solid #b00;background:#fbeeee;padding:.5rem .9rem;border-radius:4px}"
         "dl{border-top:1px solid #d8e1d4;margin-top:24px;padding-top:8px}dt{font-weight:600;margin-top:.6rem}"
         "dd{margin:0;overflow-wrap:anywhere;color:#3c5446}label{display:block;margin:1rem 0}"
         "input,select{display:block;box-sizing:border-box;width:100%;margin:8px 0;padding:8px;font:inherit}"
         "button{border:0;border-radius:6px;padding:12px 24px;background:#365d43;color:#fff;font:inherit;"
         "cursor:pointer}@media(max-width:650px){main{margin:12px;padding:22px}}")


def page(title: str, body: str) -> str:
    """One page in the family's look; `body` is already escaped HTML."""
    return (f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{escape(title)}</title><style>{STYLE}</style></head>\n"
            f'<body><main><span class="mark">&lt;^==&gt;</span>\n<h1>{escape(title)}</h1>\n{body}</main></body></html>')


def auth_settings(issuer: SQLiteIssuer, resource_url: str, required_scopes: list[str] | None = None) -> AuthSettings:
    """SDK settings for a server that is its own issuer."""
    scopes = list(issuer.policy.scopes)
    return AuthSettings(
        issuer_url=AnyHttpUrl(issuer.issuer_url), resource_server_url=AnyHttpUrl(resource_url),
        required_scopes=required_scopes if required_scopes is not None else scopes,
        client_registration_options=ClientRegistrationOptions(
            enabled=issuer.policy.dynamic_registration, valid_scopes=scopes, default_scopes=scopes),
        revocation_options=RevocationOptions(enabled=True), validate_token_resource=True)


def metadata_document(issuer: SQLiteIssuer) -> dict[str, Any]:
    """RFC 8414 metadata: the SDK's, plus what CIMD clients look for.

    Claude and ChatGPT use a metadata document only when the server says
    `client_id_metadata_document_supported` and accepts `none` at the token
    endpoint (they are public clients with PKCE).
    """
    scopes = list(issuer.policy.scopes)
    metadata = build_metadata(
        AnyHttpUrl(issuer.issuer_url), None,
        ClientRegistrationOptions(enabled=issuer.policy.dynamic_registration, valid_scopes=scopes),
        RevocationOptions(enabled=True))
    metadata.token_endpoint_auth_methods_supported = list(AUTH_METHODS)
    metadata.revocation_endpoint_auth_methods_supported = list(AUTH_METHODS)
    if issuer.policy.metadata_hosts:
        metadata.client_id_metadata_document_supported = True
    return metadata.model_dump(mode="json", exclude_none=True)


def metadata_routes(issuer: SQLiteIssuer) -> list[Route]:
    """At the root, and (RFC 8414 section 3) with the issuer's path appended when it has one."""
    from mcp.shared.auth import OAuthMetadata  # noqa: PLC0415

    handler = MetadataHandler(OAuthMetadata.model_validate(metadata_document(issuer)))
    paths = ["/.well-known/oauth-authorization-server"]
    issuer_path = urlsplit(issuer.issuer_url).path.rstrip("/")
    if issuer_path:
        paths.append(paths[0] + issuer_path)
    return [Route(path, endpoint=cors_middleware(handler.handle, ["GET", "OPTIONS"]), methods=["GET", "OPTIONS"])
            for path in paths]


def replace_metadata_routes(app: Any, issuer: SQLiteIssuer) -> None:
    """Swap the SDK's metadata route (built without CIMD) for ours, in place."""
    routes = app.router.routes
    ours = metadata_routes(issuer)
    paths = {route.path for route in ours}
    kept = [route for route in routes if getattr(route, "path", None) not in paths]
    if len(kept) == len(routes):
        raise RuntimeError("SDK app has no authorization server metadata route to replace")
    routes[:] = ours + kept


class UnderPath:
    """Serve the issuer's endpoints under the issuer URL's path as well as at the root.

    The SDK mounts /authorize, /token, ... at the app's root. Behind a proxy that
    keeps the path (for example https://host/services/<name>/...), clients call
    `<issuer>/authorize`, so `<path>/authorize` is passed on as `/authorize`.
    """

    def __init__(self, app: Any, prefix: str) -> None:
        self.app, self.prefix = app, prefix.rstrip("/")

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        path = scope.get("path", "") if scope.get("type") == "http" else ""
        if self.prefix and path.startswith(self.prefix + "/") and path[len(self.prefix):] in ISSUER_PATHS:
            inner = path[len(self.prefix):]
            scope = {**scope, "path": inner, "raw_path": inner.encode()}
        await self.app(scope, receive, send)

    def __getattr__(self, name: str) -> Any:  # the wrapped app's attributes (router, authenticated)
        return getattr(self.app, name)


def issuer_paths(issuer: SQLiteIssuer) -> tuple[str, ...]:
    """The issuer's endpoints as clients call them (with the issuer URL's path)."""
    prefix = urlsplit(issuer.issuer_url).path.rstrip("/")
    return tuple(prefix + p for p in ISSUER_PATHS)


def approval_page(consent: ConsentDetails, request_id: str, title: str, selector: str, purpose: str) -> str:
    target = urlsplit(consent.redirect_uri)
    host = target.netloc or consent.redirect_uri
    who = WHO.get(consent.origin, WHO["dynamic"]).format(host=escape(urlsplit(consent.client_id).netloc))
    warning = ""
    if consent.loopback_only:
        warning = ('<p class="warn">The code goes to a program on the computer where the client runs. '
                   'Any local program can claim this name: approve only if you just started this '
                   'connection on that computer.</p>')
    elif consent.origin == "dynamic":
        warning = ('<p class="warn">This client registered itself. Check that the destination above is where '
                   'you expect your client to be; a stranger can register any name.</p>')
    return page(title, f"""<p>{escape(purpose)} Approve only a connection you started yourself.</p>
<p>The approval goes to</p><p class="to">{escape(host)}</p>
{warning}
<dl><dt>Client</dt><dd>{escape(consent.client_name or "no name given")} ({who})</dd>
<dt>Client ID</dt><dd>{escape(consent.client_id)}</dd>
<dt>Full callback address</dt><dd>{escape(consent.redirect_uri)}</dd></dl>
<form method="post" action="approve?request={escape(request_id, quote=True)}">
{selector}
<label>Approval secret <input name="secret" type="password" autocomplete="current-password" autofocus></label>
<button type="submit">Authorize</button></form>""")


async def _bounded_form(request: Request) -> dict[str, list[str]] | None:
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > APPROVAL_BODY_BYTES:
            return None
    return parse_qs(body.decode("utf-8", errors="replace"))


def approval_route(issuer: SQLiteIssuer, title: str, purpose: str,
                   selector: Callable[[ConsentDetails], str] | None = None) -> Callable[[Request], Any]:
    """The `/approve` endpoint: GET shows the request, POST checks the secret.

    `selector(consent)` renders the target choice (a locked target or a
    select named `target`); the default shows the single target's name.
    """
    def default_selector(consent: ConsentDetails) -> str:
        name = consent.target or (next(iter(issuer.targets)) if len(issuer.targets) == 1 else None)
        if name is None:
            options = "".join(f'<option value="{escape(key, quote=True)}">{escape(t.display_name)}</option>'
                              for key, t in issuer.targets.items())
            return f'<label>For: <select name="target" required><option value="">Choose</option>{options}</select></label>'
        return (f'<p>For: <strong>{escape(issuer.targets[name].display_name)}</strong></p>'
                f'<input type="hidden" name="target" value="{escape(name, quote=True)}">')

    choose = selector or default_selector

    async def approve(request: Request) -> Response:
        request_id = request.query_params.get("request", "")
        if request.method == "GET":
            consent = await asyncio.to_thread(issuer.consent_details, request_id)
            if consent is None:
                return PlainTextResponse("Unknown or expired authorization request.", status_code=400,
                                         headers=CONSENT_HEADERS)
            return HTMLResponse(approval_page(consent, request_id, title, choose(consent), purpose),
                                headers=consent_headers(consent.redirect_uri))
        values = await _bounded_form(request)
        if values is None:
            return PlainTextResponse("Request body too large.", status_code=413, headers=CONSENT_HEADERS)
        target = values.get("target", values.get("pond", [""]))[0]
        result = await asyncio.to_thread(issuer.approve, request_id, values.get("secret", [""])[0], target or None)
        if isinstance(result, Denied):
            return HTMLResponse(page("Authorization denied", f"<p>{escape(result.message)}</p>"),
                                status_code=result.status, headers=CONSENT_HEADERS)
        return RedirectResponse(result, status_code=302, headers=CONSENT_HEADERS)

    return approve


def install(server: Any, issuer: SQLiteIssuer, title: str, purpose: str,
            selector: Callable[[ConsentDetails], str] | None = None) -> None:
    """Register `/approve` on an MCPServer before its HTTP app is built."""
    server.custom_route("/approve", methods=["GET", "POST"])(approval_route(issuer, title, purpose, selector))
