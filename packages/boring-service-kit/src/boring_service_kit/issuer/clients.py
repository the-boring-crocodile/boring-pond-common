"""Who may sign in: the client policy, metadata documents and the admin's client list.

Order of preference, as in the MCP specification of 2026-07-28:

1. **Pre-registration** -- the admin adds a client and hands out its id
   (`add_client`). For clients without metadata documents: VS Code, Copilot
   Studio, scripts.
2. **Client ID Metadata Documents (CIMD)** -- the client id is an HTTPS URL on
   an allowed host; the document there names the client and its redirect URIs.
   Claude and ChatGPT use this. Nothing on another host is ever fetched.
3. **Dynamic registration (RFC 7591)** -- off unless the installation turns it
   on; anyone can then register any redirect URI, which is why it is bounded.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx2
from mcp.shared.auth import InvalidRedirectUriError, OAuthClientInformationFull
from pydantic import AnyUrl, ValidationError

# Claude's hosted apps and Claude Code publish their documents on claude.ai,
# ChatGPT on chatgpt.com. An entry starting with "." also allows subdomains.
DEFAULT_METADATA_HOSTS = ("claude.ai", "chatgpt.com")
MAX_DOCUMENT_BYTES = 16384
FETCH_SECONDS = 5.0
# A fetched document is reused for a day, then fetched again on next use.
DOCUMENT_REFRESH_SECONDS = 86400
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})
AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")


class ClientRejected(Exception):
    """A client id that cannot be used: data for the log and the client."""


class IssuerClient(OAuthClientInformationFull):
    """A client whose loopback redirect URIs match on any port (RFC 8252 7.3).

    Native clients (Claude Code, VS Code) listen on an ephemeral port and
    declare `http://127.0.0.1/callback` or `http://localhost/callback`.
    Everything else must match exactly.
    """

    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        if redirect_uri is not None and self.redirect_uris and redirect_uri not in self.redirect_uris:
            if not any(_same_loopback(redirect_uri, registered) for registered in self.redirect_uris):
                raise InvalidRedirectUriError(f"Redirect URI '{redirect_uri}' not registered for client")
            return redirect_uri
        return super().validate_redirect_uri(redirect_uri)


def _same_loopback(requested: AnyUrl, registered: AnyUrl) -> bool:
    a, b = urlsplit(str(requested)), urlsplit(str(registered))
    return (a.scheme == b.scheme == "http" and (a.hostname or "") in LOOPBACK_HOSTS
            and a.hostname == b.hostname and a.path == b.path and a.query == b.query)


def only_loopback(client: OAuthClientInformationFull) -> bool:
    """Every redirect goes to this machine: any local program could claim to be the client."""
    return bool(client.redirect_uris) and all(
        (urlsplit(str(uri)).hostname or "") in LOOPBACK_HOSTS for uri in client.redirect_uris or [])


@dataclass(frozen=True)
class ClientPolicy:
    dynamic_registration: bool = False
    metadata_hosts: tuple[str, ...] = DEFAULT_METADATA_HOSTS
    scopes: tuple[str, ...] = ("mcp",)

    def __post_init__(self) -> None:
        for host in self.metadata_hosts:
            bare = host.removeprefix(".")
            if not bare or bare != bare.lower() or "/" in bare or ":" in bare or bare.startswith("."):
                raise ValueError(f"metadata host {host!r} must be a lowercase host name, optionally with a leading '.'")

    def is_metadata_id(self, client_id: str) -> bool:
        """Looks like a metadata document URL (whether or not it is allowed)."""
        return client_id.startswith("https://")

    def metadata_allowed(self, client_id: str) -> bool:
        try:
            parts = urlsplit(client_id)
            port = parts.port
        except ValueError:
            return False
        host = (parts.hostname or "").lower()
        if (parts.scheme != "https" or not host or parts.username or parts.password or parts.fragment
                or port not in (None, 443) or parts.path in ("", "/")
                or any(seg in (".", "..") for seg in parts.path.split("/"))):
            return False
        return any(host == entry or (entry.startswith(".") and host.endswith(entry))
                   for entry in self.metadata_hosts)


def fetch_document(url: str) -> bytes:
    """GET without redirects, proxies from the environment or cookies; bounded in time and size."""
    with httpx2.Client(timeout=FETCH_SECONDS, follow_redirects=False, trust_env=False) as client:
        with client.stream("GET", url, headers={"Accept": "application/json"}) as response:
            if response.status_code != 200:
                raise ClientRejected(f"metadata document answered HTTP {response.status_code}")
            body = b""
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > MAX_DOCUMENT_BYTES:
                    raise ClientRejected("metadata document is too large")
            return body


def client_from_document(client_id: str, body: bytes, scopes: tuple[str, ...]) -> IssuerClient:
    """Check a fetched metadata document and turn it into a public client."""
    try:
        document = json.loads(body)
    except ValueError:
        raise ClientRejected("metadata document is not JSON") from None
    if not isinstance(document, dict):
        raise ClientRejected("metadata document is not a JSON object")
    if document.get("client_id") != client_id:
        raise ClientRejected("metadata document does not name its own URL as client_id")
    if "client_secret" in document or "client_secret_expires_at" in document:
        raise ClientRejected("a metadata document must not carry a client secret")
    uris = document.get("redirect_uris")
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) and u for u in uris):
        raise ClientRejected("metadata document needs redirect_uris")
    methods = document.get("token_endpoint_auth_methods_supported")
    single = document.get("token_endpoint_auth_method")
    usable = (methods if isinstance(methods, list) else []) + ([single] if isinstance(single, str) else [])
    # A metadata client is a public client here: it authenticates with PKCE,
    # not a key. Claude's and ChatGPT's documents both offer "none".
    if usable and "none" not in usable:
        raise ClientRejected("metadata document offers no token endpoint method this server accepts")
    name = document.get("client_name")
    try:
        return IssuerClient.model_validate({
            "client_id": client_id, "client_name": name if isinstance(name, str) else None,
            "redirect_uris": uris, "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "scope": " ".join(scopes)})
    except ValidationError:
        raise ClientRejected("metadata document has invalid redirect_uris") from None


# The admin's view. Every function runs in the caller's transaction.

def add_client(db: sqlite3.Connection, name: str, redirect_uris: list[str], auth: str = "none",
               scopes: tuple[str, ...] = ("mcp",), now: float | None = None) -> dict[str, Any]:
    """Pre-register a client; a secret (when auth needs one) is shown here once."""
    if auth not in AUTH_METHODS:
        raise ValueError(f"auth must be one of {', '.join(AUTH_METHODS)}")
    if not name.strip() or len(name) > 200:
        raise ValueError("a client needs a name (at most 200 characters)")
    if not redirect_uris or len(set(redirect_uris)) != len(redirect_uris):
        raise ValueError("a client needs one or more distinct redirect URIs")
    now = time.time() if now is None else now
    client_id = "cli_" + secrets.token_urlsafe(16)
    secret = secrets.token_urlsafe(32) if auth != "none" else None
    try:
        client = IssuerClient.model_validate({
            "client_id": client_id, "client_name": name.strip(), "redirect_uris": redirect_uris,
            "token_endpoint_auth_method": auth, "client_secret": secret, "client_id_issued_at": int(now),
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "scope": " ".join(scopes)})
    except ValidationError as error:
        raise ValueError(f"invalid client: {error.errors()[0]['msg']}") from None
    for uri in client.redirect_uris or []:
        parts = urlsplit(str(uri))
        if parts.fragment or (parts.scheme != "https" and (parts.hostname or "") not in LOOPBACK_HOSTS):
            raise ValueError(f"redirect URI {uri} must be HTTPS (or HTTP on loopback) without a fragment")
    # Stored as the SDK compares it: the store file is private (0600) and a
    # stolen secret alone is useless without an approval for its client.
    db.execute("INSERT INTO clients VALUES (?, 'admin', 'active', ?, ?, ?, NULL, NULL)",
               (client_id, client.model_dump_json(exclude_none=True), now, now))
    return {"client_id": client_id, "client_secret": secret, "name": client.client_name,
            "redirect_uris": [str(u) for u in client.redirect_uris or []], "auth": auth}


def _grants(db: sqlite3.Connection, client_id: str) -> list[tuple[str, str]]:
    """(kind, key) of every record that belongs to this client."""
    found = []
    for kind, key, payload, pair in db.execute("SELECT kind, key, payload, pair FROM records"):
        if pair == "client:" + client_id:
            found.append((kind, key))
            continue
        try:
            value = json.loads(payload)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("client_id") == client_id:
            found.append((kind, key))
    return found


def list_clients(db: sqlite3.Connection, now: float | None = None) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    answer = []
    for client_id, origin, state, payload, created, _, last_used, expires in db.execute(
            "SELECT * FROM clients ORDER BY created"):
        client = json.loads(payload)
        live = [k for k, _ in _grants(db, client_id)]
        answer.append({
            "client_id": client_id, "origin": origin, "state": state, "name": client.get("client_name"),
            "redirect_hosts": sorted({urlsplit(u).netloc for u in client.get("redirect_uris") or []}),
            "created": created, "last_used": last_used, "expires": expires,
            "refresh_tokens": live.count("refresh"), "access_tokens": live.count("access")})
    return answer


def remove_client(db: sqlite3.Connection, client_id: str) -> bool:
    """Remove a client and everything it was granted; its connection stops at once."""
    db.executemany("DELETE FROM records WHERE kind=? AND key=?", _grants(db, client_id))
    return db.execute("DELETE FROM clients WHERE client_id=?", (client_id,)).rowcount == 1


def prune_clients(db: sqlite3.Connection, idle_days: float, apply: bool = False,
                  now: float | None = None) -> list[dict[str, Any]]:
    """Clients not used for idle_days that hold no live refresh token.

    Registered clients that were never used expire by themselves; this finds
    active ones that were abandoned (a removed connector). Admin clients are
    kept: the admin added them and removes them by name.
    """
    now = time.time() if now is None else now
    stale = [c for c in list_clients(db, now)
             if c["origin"] != "admin" and c["state"] == "active" and c["refresh_tokens"] == 0
             and (c["last_used"] or c["created"]) < now - idle_days * 86400]
    if apply:
        for client in stale:
            remove_client(db, client["client_id"])
    return stale


Fetch = Callable[[str], bytes]
