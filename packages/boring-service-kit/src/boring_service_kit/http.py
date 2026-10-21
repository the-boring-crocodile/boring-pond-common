"""Putting a server on the network: its own issuer, an external one, access keys; Pond registration optional."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, cast
from urllib.parse import urlsplit
from mcp.server.auth.provider import ProviderTokenVerifier
from mcp.server.auth.settings import AuthSettings
from boring_registry import POND_CALL_MARGIN_SECONDS, POND_CALL_SECONDS, Watchdog
from .access import AccessPolicy
from .issuer import DEFAULT_METADATA_HOSTS, ClientPolicy, SQLiteIssuer, Target
from .issuer.web import UnderPath, auth_settings, install, issuer_paths, replace_metadata_routes
from .keys import EitherVerifier, KeyVerifier
from .pond import (ControlPlane, IntrospectionVerifier, Registration, read_secret, run, wrap_app)


def _url(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a URL")
    parsed = urlsplit(value)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query
            or parsed.scheme not in {"https", "http"}):
        raise ValueError(f"{name} must be an absolute URL without credentials, query or fragment")
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(f"{name} must use HTTPS (HTTP allowed only for loopback fixtures)")
    return value.rstrip("/")


LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class ServiceVerifier(IntrospectionVerifier):
    def __init__(self, authority: ControlPlane, own_url: str, endpoint: str,
                 policy: AccessPolicy, scopes: list[str]) -> None:
        super().__init__(authority, own_url)
        self.endpoint = endpoint
        self.policy = policy
        self.scopes = scopes

    def accept(self, token: str, answer: Any, now: float) -> Any:
        if not isinstance(answer, dict):
            return None
        # Introspection is an authenticated answer from the configured issuer.
        issuer = answer.get("iss", self.policy.issuer)
        if not isinstance(issuer, str) or issuer.rstrip("/") != self.policy.issuer:
            return None
        audience = answer.get("aud")
        if isinstance(audience, list) and self.own_url in audience:
            answer = {**answer, "aud": self.own_url}
        access = super().accept(token, answer, now)
        if access is None:
            return None
        caller = self.policy.resolve(access.subject or "")
        scope = answer.get("scope", "")
        if not isinstance(scope, str) or not set(self.scopes) <= set(scope.split()):
            return None
        if caller is None:
            return None
        access.scopes = scope.split()
        access.claims = {"boring_caller": asdict(caller)}
        return access


class ServiceConfig:
    """Strict JSON config: how a server is put on the network. Relative files resolve next to the config.

    | key | tokens come from |
    |---|---|
    | `issuer` | this server: approval secret, pre-registered and metadata-document clients |
    | `oauth` | an external issuer, checked by introspection (a Crocodile, Keycloak, Entra) |
    | `keys` | nobody: static access keys (combines with either of the above) |
    | `pond` | (heartbeat registration with a Crocodile, beside `oauth`) |

    `access` lists the subjects an issuer may vouch for, each a reader or writer.
    """
    KEYS = {"url", "bind", "port", "oauth", "issuer", "access", "keys", "pond"}
    auth: dict[str, Any]
    token_verifier: Any  # the bearer check, also for a server's own non-MCP routes
    ISSUER_KEYS = {"url", "database", "approval_secret_file", "subject", "name", "dynamic_registration",
                   "metadata_document_hosts"}

    def __init__(self, path: Path) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) - self.KEYS:
            raise ValueError(f"config supports {', '.join(sorted(self.KEYS))}")
        if not {"oauth", "issuer", "keys"} & set(data):
            raise ValueError("config needs issuer, oauth or keys")
        if "oauth" in data and "issuer" in data:
            raise ValueError("oauth and issuer exclude each other: tokens come from one issuer")
        if "access" in data and not {"oauth", "issuer"} & set(data):
            raise ValueError("access lists OAuth subjects; with keys only, each key names its person and role")
        self._listen(data)
        scopes = self._oauth_scopes(data["oauth"]) if "oauth" in data else ["mcp"] if "issuer" in data else []
        self.keys: KeyVerifier | None = None
        if "keys" in data:
            keys = data["keys"]
            if not isinstance(keys, dict) or set(keys) != {"file"}:
                raise ValueError("keys needs exactly file (the key file, see boring-service-keys)")
            self.keys = KeyVerifier(self.file(path, keys["file"], "keys file"), self.url, scopes)
        self.verifier: ServiceVerifier | None = None
        self.issuer: SQLiteIssuer | None = None
        if "oauth" in data:
            self._external(path, data["oauth"], data.get("access", []), scopes)
        elif "issuer" in data:
            self.issuer = self._issuer(path, data["issuer"], data.get("access", []))
            self.auth = {"auth_server_provider": self.issuer, "auth": auth_settings(self.issuer, self.url)}
            # For a server's own non-MCP routes that take the same bearer (fishmarket's transfers).
            self.token_verifier = ProviderTokenVerifier(self.issuer)
        else:  # keys only: no authority to name; resource metadata points at the service itself
            self.token_verifier = EitherVerifier(self.keys, None)
            self.auth = {"token_verifier": self.token_verifier,
                         "auth": AuthSettings(issuer_url=cast(Any, self.url), resource_server_url=cast(Any, self.url),
                                              validate_token_resource=True)}
        self.watchdog = self._pond(path, data["pond"]) if data.get("pond") is not None else None

    def _listen(self, data: dict[str, Any]) -> None:
        self.url = _url(data["url"], "url")
        self.bind = data.get("bind", "127.0.0.1")
        self.port = data.get("port", urlsplit(self.url).port or 8000)
        if not isinstance(self.bind, str) or not self.bind:
            raise ValueError("bind must be a host")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("port must be 1..65535")
        if urlsplit(self.url).scheme != "https" and self.bind not in LOOPBACK:
            raise ValueError("bearer authentication over plain HTTP needs a loopback bind; use HTTPS otherwise")

    @staticmethod
    def _oauth_scopes(oauth: Any) -> list[str]:
        if not isinstance(oauth, dict) or set(oauth) - {"issuer", "introspection_url", "client_id", "secret_file", "scopes"}:
            raise ValueError("invalid oauth configuration")
        scopes = oauth.get("scopes", [])
        if not isinstance(scopes, list) or any(not isinstance(s, str) or not s for s in scopes):
            raise ValueError("scopes must be a list of nonempty strings")
        return scopes

    def _external(self, path: Path, oauth: dict[str, Any], access: Any, scopes: list[str]) -> None:
        """Tokens from another issuer, checked by introspection (a Crocodile, Keycloak, Entra)."""
        issuer = _url(oauth["issuer"], "issuer")
        endpoint = _url(oauth["introspection_url"], "introspection_url")
        if not isinstance(oauth["client_id"], str) or not oauth["client_id"]:
            raise ValueError("OAuth client_id must be nonempty")
        self.policy = AccessPolicy(issuer, access)
        self.authority = ControlPlane(issuer, oauth["client_id"], read_secret(self.file(path, oauth["secret_file"])))
        self.verifier = ServiceVerifier(self.authority, self.url, endpoint, self.policy, scopes)
        self.token_verifier = EitherVerifier(self.keys, self.verifier)
        self.auth = {"token_verifier": self.token_verifier,
                     "auth": AuthSettings(issuer_url=cast(Any, issuer), resource_server_url=cast(Any, self.url),
                                          validate_token_resource=True, required_scopes=scopes or None)}

    def _pond(self, path: Path, pond: Any) -> Watchdog:
        if not isinstance(pond, dict) or set(pond) != {"url", "client_id", "secret_file"}:
            raise ValueError("pond requires url, client_id and secret_file")
        return Watchdog(Registration(ControlPlane(_url(pond["url"], "pond url"), pond["client_id"],
                                                  read_secret(self.file(path, pond["secret_file"])))))

    def _issuer(self, path: Path, spec: Any, access: Any) -> SQLiteIssuer:
        if not isinstance(spec, dict) or set(spec) - self.ISSUER_KEYS:
            raise ValueError(f"issuer supports {', '.join(sorted(self.ISSUER_KEYS))}")
        own = urlsplit(self.url)
        issuer_url = _url(spec.get("url", f"{own.scheme}://{own.netloc}"), "issuer url")
        subject, name = spec.get("subject"), spec.get("name", spec.get("subject"))
        if not isinstance(subject, str) or not subject or not isinstance(name, str) or not name:
            raise ValueError("issuer needs a subject: who the tokens it issues speak for")
        dynamic = spec.get("dynamic_registration", False)
        hosts = spec.get("metadata_document_hosts", list(DEFAULT_METADATA_HOSTS))
        if not isinstance(dynamic, bool):
            raise ValueError("dynamic_registration must be true or false")
        if not isinstance(hosts, list) or any(not isinstance(h, str) for h in hosts):
            raise ValueError("metadata_document_hosts must be a list of host names")
        self.policy = AccessPolicy(issuer_url, access)
        if self.policy.resolve(subject) is None:
            raise ValueError("issuer.subject must be listed in access, with its role")

        def claims(token_subject: str) -> dict[str, Any] | None:
            caller = self.policy.resolve(token_subject)
            return None if caller is None else {"boring_caller": asdict(caller)}

        target = Target(subject, name, self.file(path, spec.get("approval_secret_file"), "approval_secret_file"))
        return SQLiteIssuer(issuer_url, self.file(path, spec.get("database"), "issuer database").resolve(),
                            {self.url: "owner"}, {"owner": target},
                            ClientPolicy(dynamic_registration=dynamic, metadata_hosts=tuple(hosts)),
                            claims=claims, also=self.keys)

    @staticmethod
    def file(config: Path, value: Any, what: str = "secret_file") -> Path:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{what} must be a path")
        p = Path(value)
        return p if p.is_absolute() else config.resolve().parent / p

    @property
    def call_limit(self) -> float | None:
        """Seconds one tool call may take: the Pond's bound less the answer's margin when
        this service is registered with a Pond; None (no bound) otherwise, e.g. for Copilot
        on loopback, which waits as long as the call takes."""
        return POND_CALL_SECONDS - POND_CALL_MARGIN_SECONDS if self.watchdog is not None else None

    @property
    def on_discover(self) -> Any:
        return self.watchdog.pinged if self.watchdog is not None else None

    def app(self, server: Any) -> Any:
        """The server's HTTP app, with the approval page and metadata when it is its own issuer."""
        issuer = self.issuer
        if issuer is None:
            return wrap_app(server, self.url, discover_bypass=self.watchdog is not None)
        subject = next(iter(issuer.targets.values())).display_name
        install(server, issuer, f"Authorize {server.name}",
                f"Authorize this client to use {server.name}'s tools as {subject}.")
        app = wrap_app(server, self.url, discover_bypass=self.watchdog is not None,
                       customize=lambda app: replace_metadata_routes(app, issuer))
        prefix = urlsplit(issuer.issuer_url).path
        return UnderPath(app, prefix) if prefix.strip("/") else app

    @property
    def issuer_paths(self) -> tuple[str, ...]:
        """Paths a server that routes requests itself must send to the SDK app (none without an issuer)."""
        return issuer_paths(self.issuer) if self.issuer is not None else ()

    async def run(self, server: Any) -> None:
        await run(self.app(server), self.watchdog, self.bind, self.port)

    def serve(self, make_server: Callable[[dict[str, Any], Any], Any]) -> None:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
        asyncio.run(self.run(make_server(self.auth, self.on_discover)))


def add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, help="HTTP service configuration JSON (OAuth and/or access keys)")
