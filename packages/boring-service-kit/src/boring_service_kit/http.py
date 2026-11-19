"""Putting a service on the network with one Crocodile, keys or own-issuer mode."""
from __future__ import annotations

import argparse
import asyncio
import json
import ipaddress
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, cast
from urllib.parse import urlsplit
from mcp.server.auth.provider import ProviderTokenVerifier
from mcp.server.auth.settings import AuthSettings
from boring_registry import POND_CALL_MARGIN_SECONDS, POND_CALL_SECONDS, Watchdog
from .access import verified_caller
from .issuer import DEFAULT_METADATA_HOSTS, ClientPolicy, SQLiteIssuer, Target
from .issuer.provider import read_approval_secret
from .issuer.web import UnderPath, issuer_auth_settings, configure_issuer_routes, install, issuer_paths
from .keys import KeyVerifier
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


def _loopback_bind(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ServiceVerifier(IntrospectionVerifier):
    """Crocodile owns the role; only its authenticated introspection can supply it."""
    def accept(self, token: str, answer: Any, now: float) -> Any:
        if not isinstance(answer, dict):
            return None
        issuer = self.control_plane.url
        received = answer.get("iss", issuer)
        if not isinstance(received, str) or received.rstrip("/") != issuer:
            return None
        audience = answer.get("aud")
        if isinstance(audience, list) and self.own_url in audience:
            answer = {**answer, "aud": self.own_url}
        access = super().accept(token, answer, now)
        if access is None:
            return None
        try:
            caller = verified_caller(issuer, access.subject or "", answer.get("role"))
        except ValueError:
            return None
        access.claims = {"boring_caller": asdict(caller)}
        return access


@dataclass(frozen=True)
class ServiceInfo:
    """Validated frontend metadata, without credentials or runtime behavior."""
    url: str
    bind: str
    port: int
    client_id: str | None
    authority_url: str | None


class ServiceConfig:
    """One explicit auth mode: Crocodile grants, access keys or an own issuer.

    Crocodile uses one authority for introspection and heartbeat registration.
    Keys carry their role; an own issuer declares one role for approved clients.
    """
    KEYS = {"url", "mode", "crocodile", "issuer", "keys", "bind", "port", "behind_proxy"}
    auth: dict[str, Any]
    token_verifier: Any  # the bearer check, also for a server's own non-MCP routes
    ISSUER_KEYS = {"url", "database", "approval_secret_file", "subject", "name", "dynamic_registration",
                   "metadata_document_hosts", "role"}

    def __init__(self, path: Path) -> None:
        self._configure(path, initialize=True)

    @classmethod
    def validate(cls, path: Path) -> None:
        """Use startup's parser without opening issuer state or starting a server.

        Configuration and referenced client secrets are read. Database contents,
        filesystem write permissions and network reachability are not tested.
        The private validation instance never becomes a runnable service.
        """
        cls.inspect(path)

    @classmethod
    def inspect(cls, path: Path) -> ServiceInfo:
        """Validate read-only and return the same safe metadata runtime uses."""
        config = cls.__new__(cls)
        config._configure(path, initialize=False)
        return config.info()

    @classmethod
    def inspect_value(cls, data: dict[str, Any], path: Path) -> ServiceInfo:
        """Validate a planned config with startup's parser, without writing it.

        Relative credentials resolve against the eventual config path.
        No database is opened and no runtime state is created.
        """
        config = cls.__new__(cls)
        config._configure_value(data, path, initialize=False)
        return config.info()

    def info(self) -> ServiceInfo:
        authority = getattr(self, "authority", None)
        return ServiceInfo(self.url, self.bind, self.port,
                           authority.client_id if authority is not None else None,
                           authority.url if authority is not None else None)

    def _configure(self, path: Path, *, initialize: bool) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        self._configure_value(data, path, initialize=initialize)

    def _configure_value(self, data: Any, path: Path, *, initialize: bool) -> None:
        if isinstance(data, dict):
            replacements = {"access": "role on the Crocodile grant, key or own issuer",
                            "oauth": "mode=crocodile with one crocodile authority block",
                            "pond": "mode=crocodile; registration reuses its authority"}
            old = sorted(set(data) & set(replacements))
            if old:
                raise ValueError("; ".join(key + " is replaced by " + replacements[key] for key in old))
        if not isinstance(data, dict) or set(data) - self.KEYS:
            raise ValueError(f"config supports {', '.join(sorted(self.KEYS))}")
        modes = {"crocodile", "keys", "issuer"}
        self.mode = data.get("mode")
        if not isinstance(self.mode, str) or self.mode not in modes:
            raise ValueError("mode must select exactly one of crocodile, keys or issuer")
        if set(data) & modes != {self.mode}:
            raise ValueError("configure exactly one authentication mode and its matching block")
        self._listen(data, path)
        self.keys: KeyVerifier | None = None
        self.verifier: ServiceVerifier | None = None
        self.issuer: SQLiteIssuer | None = None
        self.watchdog: Watchdog | None = None
        if self.mode == "crocodile":
            self._crocodile(path, data["crocodile"])
        elif self.mode == "issuer":
            self.issuer = self._issuer(path, data["issuer"], initialize=initialize)
            if self.issuer is not None:
                self.auth["auth_server_provider"] = self.issuer
                self.token_verifier = ProviderTokenVerifier(self.issuer)
        else:
            keys = data["keys"]
            if not isinstance(keys, dict) or set(keys) != {"file"}:
                raise ValueError("keys needs exactly file (the key file, see boring-service-keys)")
            self.keys = KeyVerifier(self.file(path, keys["file"], "keys file"), self.url)
            self.token_verifier = self.keys
            self.auth = {"token_verifier": self.token_verifier,
                         "auth": AuthSettings(issuer_url=cast(Any, self.url), resource_server_url=cast(Any, self.url),
                                              validate_token_resource=True)}

    def _listen(self, data: dict[str, Any], path: Path) -> None:
        self.url = _url(data["url"], "url")
        container = os.environ.get("BORING_SERVICE_CONTAINER")
        if container not in {None, "1"}:
            raise ValueError("BORING_SERVICE_CONTAINER must be 1 in service images, or absent for native runs")
        raw = os.environ.get("BORING_SERVICE_LISTENERS")
        if container == "1":
            old = sorted(set(data) & {"bind", "port", "behind_proxy"})
            if old:
                raise ValueError("; ".join(key + " is replaced by Compose environment BORING_SERVICE_LISTENERS"
                                           for key in old))
            source = str(path.resolve())
            try:
                listeners = json.loads(raw or "{}")
            except ValueError:
                raise ValueError("BORING_SERVICE_LISTENERS must be a JSON object keyed by absolute config path") from None
            if not isinstance(listeners, dict):
                raise ValueError("BORING_SERVICE_LISTENERS must be a JSON object keyed by absolute config path")
            for name, listener in listeners.items():
                if (not Path(name).is_absolute() or not isinstance(listener, dict)
                        or set(listener) != {"bind", "port"}
                        or not isinstance(listener["bind"], str) or not listener["bind"]
                        or type(listener["port"]) is not int or not 1 <= listener["port"] <= 65535):
                    raise ValueError("BORING_SERVICE_LISTENERS entries need an absolute config path and exactly bind (host), port (1..65535)")
            if source not in listeners:
                raise ValueError("BORING_SERVICE_LISTENERS needs an address for " + source)
            self.bind, self.port = listeners[source]["bind"], listeners[source]["port"]
            return
        if raw is not None:
            raise ValueError("BORING_SERVICE_LISTENERS requires BORING_SERVICE_CONTAINER=1")
        self.bind = data.get("bind", "127.0.0.1")
        self.port = data.get("port", urlsplit(self.url).port or 8000)
        if not isinstance(self.bind, str) or not self.bind:
            raise ValueError("bind must be a host")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("port must be 1..65535")
        self.behind_proxy = data.get("behind_proxy", False)
        if type(self.behind_proxy) is not bool:
            raise ValueError("behind_proxy must be boolean")
        # The public URL describes the proxy, not this plaintext listener.
        if not _loopback_bind(self.bind) and not self.behind_proxy:
            raise ValueError("plain HTTP bearer authentication needs a loopback bind; "
                             "a trusted proxy on another host or container network requires "
                             "explicit behind_proxy=true and restricted network access")

    def _crocodile(self, path: Path, authority: Any) -> None:
        if not isinstance(authority, dict) or set(authority) != {"url", "client_id", "secret_file"}:
            raise ValueError("crocodile needs exactly url, client_id and secret_file")
        issuer = _url(authority["url"], "Crocodile url")
        if not isinstance(authority["client_id"], str) or not authority["client_id"]:
            raise ValueError("Crocodile client_id must be nonempty")
        self.authority = ControlPlane(issuer, authority["client_id"],
                                      read_secret(self.file(path, authority["secret_file"])))
        self.verifier = ServiceVerifier(self.authority, self.url)
        self.token_verifier = self.verifier
        self.auth = {"token_verifier": self.token_verifier,
                     "auth": AuthSettings(issuer_url=cast(Any, issuer), resource_server_url=cast(Any, self.url),
                                          validate_token_resource=True)}
        self.watchdog = Watchdog(Registration(self.authority))

    def _issuer(self, path: Path, spec: Any, *, initialize: bool) -> SQLiteIssuer | None:
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
        caller = verified_caller(issuer_url, subject, spec.get("role"))

        def claims(token_subject: str) -> dict[str, Any] | None:
            return {"boring_caller": asdict(caller)} if token_subject == subject else None

        target = Target(subject, name, self.file(path, spec.get("approval_secret_file"), "approval_secret_file"))
        database = self.file(path, spec.get("database"), "issuer database").resolve()
        policy = ClientPolicy(dynamic_registration=dynamic, metadata_hosts=tuple(hosts))
        # SDK settings are part of validation too, before any issuer-store effect.
        self.auth = {"auth": issuer_auth_settings(issuer_url, self.url, policy)}
        if not initialize:
            return None
        read_approval_secret(target.secret_file)
        return SQLiteIssuer(issuer_url, database, {self.url: "owner"}, {"owner": target},
                            policy, claims=claims)

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
                       customize=lambda app: configure_issuer_routes(app, issuer))
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
    parser.add_argument("--config", type=Path, help="HTTP service configuration JSON (one Crocodile, keys or own-issuer mode)")
