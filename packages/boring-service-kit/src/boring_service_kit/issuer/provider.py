"""An OAuth 2.1 authorization server in one SQLite file, for the MCP SDK.

One issuer protects one or more resources (MCP URLs). A sign-in is approved
for a *target*: a subject (who the token speaks for) with its own approval
secret. A service has one target, its owner; the Crocodile has one per Pond.
A resource either names its target or leaves the choice to the approval page.

The flow, with what each step stores:

1. The client gets a client id: pre-registered by the admin, a metadata
   document URL on an allowed host (fetched, cached), or -- when enabled --
   dynamic registration. (`clients`)
2. `/authorize` checks the client, its redirect URI and the resource and parks
   the request for five minutes. (`records`: pending)
3. `/approve` shows where the code will go and asks for the target's approval
   secret; five wrong secrets cancel the request. (`records`: code)
4. `/token` trades the code (with the PKCE verifier) for an access token (1 h)
   and a refresh token (14 days, rotated on use). The client becomes active.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, Callable, NamedTuple

import httpx2
from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError, OAuthAuthorizationServerProvider,
    RefreshToken, RegistrationError, TokenError, TokenVerifier, construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, BaseModel, ValidationError

from .clients import (DOCUMENT_REFRESH_SECONDS, ClientPolicy, ClientRejected, Fetch, IssuerClient,
                      client_from_document, fetch_document, only_loopback)
from .store import Initializer, Transaction, backup, initialize_issuer_store, private_file, secret_key

log = logging.getLogger("boring_service_kit.issuer")

PENDING_SECONDS = 300
CODE_SECONDS = 300
ACCESS_SECONDS = 3600
REFRESH_SECONDS = 14 * 86400  # 14 days unused before the approval secret is asked again
# Wrong approval secrets one pending request takes; the last one cancels it.
# There is no global pause: the approval secret is long and random, so guessing
# is not the threat, and a pause anyone can trigger would lock out the owner.
MAX_APPROVAL_ATTEMPTS = 5
# Clients nobody approved yet (dynamic registrations, cached metadata
# documents) expire after a day, and only those count against a cap per
# origin. An active client exists only after an approval with the secret.
UNUSED_CLIENT_SECONDS = 86400
MAX_UNUSED_CLIENTS = 1000
MAX_CLIENT_BYTES = 8192
# Open authorization requests: a few per client and a cap overall. Past
# either, the oldest request makes room instead of the new one being refused:
# Claude's and ChatGPT's client ids and callbacks are public, so anyone can
# open requests in their name, and a refusal would lock the owner out.
MAX_PENDING_PER_CLIENT = 5
MAX_PENDING = 100
# A metadata document that could not be fetched or used is not fetched again
# for a minute, so a client id that keeps failing costs one outbound request a
# minute, not one per /authorize. In memory, bounded; the oldest entry makes room.
FAILED_DOCUMENT_SECONDS = 60
MAX_FAILED_DOCUMENTS = 1000


@dataclass(frozen=True)
class Target:
    """What an approval grants: tokens for `subject`, if you know `secret_file`'s secret."""
    subject: str
    display_name: str
    secret_file: Path


class ConsentDetails(NamedTuple):
    """Public facts about one live authorization request, for the approval page."""
    client_id: str
    client_name: str | None
    redirect_uri: str
    target: str | None  # locked by the resource, or None: the page asks
    origin: str  # admin, metadata or dynamic
    loopback_only: bool


class Denied(NamedTuple):
    """Why an approval did not happen: data for the approval page."""
    status: int
    message: str


class Pending(BaseModel):
    client: OAuthClientInformationFull
    params: AuthorizationParams
    expires_at: float
    origin: str = "dynamic"


def canonical_resource(value: str | None) -> str | None:
    """Compare resource URLs as URLs (case, default port), ignoring one trailing slash."""
    if not value:
        return None
    try:
        return str(AnyHttpUrl(value)).removesuffix("/")
    except ValidationError:
        return None


def _sdk_async(function: Callable[..., Any]) -> Callable[..., Any]:
    """SDK store operations run off the event loop, with a connection per operation."""
    @wraps(function)
    async def call(*args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(function, *args, **kwargs)
    return call


class _PrunedTransaction(Transaction):
    def __enter__(self) -> sqlite3.Connection:
        db = super().__enter__()
        try:
            now = time.time()
            db.execute("DELETE FROM records WHERE expires IS NOT NULL AND expires<=?", (now,))
            db.execute("DELETE FROM clients WHERE expires IS NOT NULL AND expires<=?", (now,))
        except BaseException:
            db.execute("ROLLBACK")
            db.close()
            raise
        return db


class SQLiteIssuer(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    # Record kinds whose key is a bearer value -> the payload field repeating it.
    secret_kinds: dict[str, str | None] = {"pending": None, "code": "code", "access": "token", "refresh": "token"}

    def __init__(self, issuer_url: str, database_file: Path, resources: dict[str, str | None],
                 targets: dict[str, Target], policy: ClientPolicy = ClientPolicy(), *,
                 initialize: Initializer = initialize_issuer_store, fetch: Fetch = fetch_document,
                 claims: Callable[[str], dict[str, Any] | None] | None = None,
                 also: TokenVerifier | None = None) -> None:
        """`resources`: canonical resource URL -> target name, or None (the page asks).
        `claims(subject)`: extra claims for an access token, or None to refuse it.
        `also`: another verifier for tokens this issuer did not mint (access keys)."""
        self.issuer_url = issuer_url.rstrip("/")
        self.database_file = database_file
        self.resources = {str(canonical_resource(url)): name for url, name in resources.items()}
        if any(name is not None and name not in targets for name in self.resources.values()):
            raise ValueError("every resource names a configured target")
        self.targets = targets
        self.policy = policy
        self.fetch = fetch
        self.claims = claims
        self.also = also
        # request_id -> (wrong secrets so far, when the pending request expires).
        # In memory: one process; a restart resets the counts.
        self.wrong_secrets: dict[str, tuple[int, float]] = {}
        self._approval_lock = threading.RLock()
        # client id -> until when its metadata document is not fetched again.
        self.failed_documents: dict[str, float] = {}
        self._failed_documents_lock = threading.Lock()
        private_file(database_file)
        with Transaction(self.database_file) as db:
            try:
                initialize(db)
            except ValueError as error:
                raise RuntimeError(str(error)) from None

    @property
    def subjects(self) -> set[str]:
        return {target.subject for target in self.targets.values()}

    # Storage helpers.

    @staticmethod
    def _new_token(prefix: str) -> str:
        return prefix + secrets.token_urlsafe(32)

    def _transaction(self) -> _PrunedTransaction:
        return _PrunedTransaction(self.database_file)

    def _read_database(self, timeout: float = .1) -> closing[sqlite3.Connection]:
        # Short on the event loop; worker-thread callers (_load) wait longer
        # for a writer instead of failing a token check.
        return closing(sqlite3.connect(self.database_file.as_uri() + "?mode=ro",
                                       uri=True, timeout=timeout, isolation_level=None))

    def _stored_key(self, kind: str, key: str) -> str:
        return secret_key(key) if kind in self.secret_kinds else key

    def _put(self, db: sqlite3.Connection, kind: str, key: str, model: BaseModel,
             pair: str | None = None, expires: float | None = None) -> None:
        field = self.secret_kinds.get(kind)
        if field:
            model = model.model_copy(update={field: ""})
        db.execute("INSERT INTO records VALUES (?, ?, ?, ?, ?)", (
            kind, self._stored_key(kind, key), model.model_dump_json(),
            expires if expires is not None else getattr(model, "expires_at", None), pair))

    def _get(self, db: sqlite3.Connection, kind: str, key: str, model: type[Any]) -> Any:
        row = db.execute("SELECT payload FROM records WHERE kind=? AND key=? AND (expires IS NULL OR expires>?)",
                         (kind, self._stored_key(kind, key), time.time())).fetchone()
        if row is None:
            return None
        value = model.model_validate_json(row[0])
        field = self.secret_kinds.get(kind)
        # The caller presented the value it was looked up by; restore it in memory.
        return value.model_copy(update={field: key}) if field else value

    def _load(self, kind: str, key: str, model: type[Any]) -> Any:
        with self._read_database(timeout=2.0) as db:
            return self._get(db, kind, key, model)

    # The approval page's questions.

    def consent_details(self, request_id: str) -> ConsentDetails | None:
        pending = self._load("pending", request_id, Pending)
        if pending is None:
            return None
        return ConsentDetails(pending.client.client_id, pending.client.client_name, str(pending.params.redirect_uri),
                              self.resources.get(str(canonical_resource(pending.params.resource))),
                              pending.origin, only_loopback(pending.client))

    def has_pending(self, request_id: str) -> bool:
        return self._load("pending", request_id, Pending) is not None

    def pending_target(self, request_id: str) -> str | None:
        """The target a pending request is locked to by its resource, if any."""
        pending = self._load("pending", request_id, Pending)
        return None if pending is None else self.resources.get(str(canonical_resource(pending.params.resource)))

    # Clients.

    def _client_row(self, db: sqlite3.Connection, client_id: str) -> tuple[str, str, str, float] | None:
        row = db.execute("SELECT origin, state, payload, refreshed FROM clients WHERE client_id=? "
                         "AND (expires IS NULL OR expires>?)", (client_id, time.time())).fetchone()
        return None if row is None else (row[0], row[1], row[2], row[3])

    def client_origin(self, client_id: str) -> str | None:
        with self._read_database(timeout=2.0) as db:
            row = self._client_row(db, client_id)
        return row[0] if row else ("metadata" if self.policy.metadata_allowed(client_id) else None)

    @_sdk_async
    def get_client(self, client_id: str) -> IssuerClient | None:
        with self._read_database(timeout=2.0) as db:
            row = self._client_row(db, client_id)
        if row is not None and row[0] != "metadata":
            return IssuerClient.model_validate_json(row[2])
        if not self.policy.is_metadata_id(client_id):
            return None
        if not self.policy.metadata_allowed(client_id):
            log.warning("client id %s: metadata documents on this host are not allowed", client_id)
            return None
        if row is not None and row[3] > time.time() - DOCUMENT_REFRESH_SECONDS:
            return IssuerClient.model_validate_json(row[2])
        try:
            client = self._document_client(client_id)
        except (ClientRejected, httpx2.HTTPError) as error:
            log.warning("client id %s: metadata document refused: %s", client_id, error)
            # A client already in use keeps working on its last good document
            # while the host has trouble; a new one does not start without one.
            return IssuerClient.model_validate_json(row[2]) if row is not None and row[1] == "active" else None
        self._remember_document(client)
        return client

    def _document_client(self, client_id: str) -> IssuerClient:
        """Fetch and check a metadata document, unless it failed within FAILED_DOCUMENT_SECONDS.

        Raises ClientRejected or httpx2.HTTPError; a failure is remembered."""
        now = time.time()
        with self._failed_documents_lock:
            if self.failed_documents.get(client_id, 0.0) > now:
                raise ClientRejected("metadata document failed recently; not fetched again yet")
        try:
            return client_from_document(client_id, self.fetch(client_id), self.policy.scopes)
        except (ClientRejected, httpx2.HTTPError):
            with self._failed_documents_lock:
                failed = {key: until for key, until in self.failed_documents.items() if until > now}
                failed.pop(client_id, None)
                while len(failed) >= MAX_FAILED_DOCUMENTS:
                    del failed[next(iter(failed))]  # insertion order: the oldest failure
                failed[client_id] = now + FAILED_DOCUMENT_SECONDS
                self.failed_documents = failed
            raise

    def _remember_document(self, client: IssuerClient) -> None:
        now = time.time()
        with self._transaction() as db:
            if db.execute("UPDATE clients SET payload=?, refreshed=? WHERE client_id=?",
                          (client.model_dump_json(exclude_none=True), now, client.client_id)).rowcount:
                return
            unused = db.execute("SELECT COUNT(*) FROM clients WHERE origin='metadata' AND expires IS NOT NULL"
                                ).fetchone()[0]
            # Past the cap the document is used without being kept; the first
            # approval stores the client anyway.
            if unused < MAX_UNUSED_CLIENTS:
                db.execute("INSERT INTO clients VALUES (?, 'metadata', 'registered', ?, ?, ?, NULL, ?)",
                           (client.client_id, client.model_dump_json(exclude_none=True), now, now,
                            now + UNUSED_CLIENT_SECONDS))

    @_sdk_async
    def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not self.policy.dynamic_registration:
            raise RegistrationError("invalid_client_metadata", "Dynamic client registration is disabled here")
        if len(client_info.model_dump_json()) > MAX_CLIENT_BYTES:
            raise RegistrationError("invalid_client_metadata", "Client metadata is too large")
        now = time.time()
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM clients WHERE client_id=?", (client_info.client_id,)).fetchone():
                raise RegistrationError("invalid_client_metadata", "Client already registered")
            if db.execute("SELECT COUNT(*) FROM clients WHERE origin='dynamic' AND expires IS NOT NULL"
                          ).fetchone()[0] >= MAX_UNUSED_CLIENTS:
                raise RegistrationError("invalid_client_metadata", "Too many registered clients; try again later")
            db.execute("INSERT INTO clients VALUES (?, 'dynamic', 'registered', ?, ?, ?, NULL, ?)",
                       (client_info.client_id, client_info.model_dump_json(), now, now, now + UNUSED_CLIENT_SECONDS))

    # Authorization.

    @_sdk_async
    def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if canonical_resource(params.resource) not in self.resources:
            raise AuthorizeError("invalid_target", "resource must be one of this server's MCP URLs")
        request_id = self._new_token("req_")
        origin = self.client_origin(client.client_id) or "dynamic"
        with self._transaction() as db:
            # A pending row's pair names its client ("client:<id>"); token
            # pairs are "pair_..." values, so the two never meet.
            owner = "client:" + client.client_id
            self._make_room_for_pending(db, owner)
            self._put(db, "pending", request_id, Pending(
                client=client, params=params, expires_at=time.time() + PENDING_SECONDS, origin=origin), pair=owner)
        return f"{self.issuer_url}/approve?request={request_id}"

    @staticmethod
    def _make_room_for_pending(db: sqlite3.Connection, owner: str) -> None:
        """Drop the oldest open requests so one more fits: for this client, then overall."""
        for scope, args, cap in ((" AND pair=?", (owner,), MAX_PENDING_PER_CLIENT), ("", (), MAX_PENDING)):
            surplus = db.execute("SELECT COUNT(*) FROM records WHERE kind='pending'" + scope,
                                 args).fetchone()[0] - cap + 1
            if surplus > 0:
                db.execute("DELETE FROM records WHERE rowid IN (SELECT rowid FROM records "
                           "WHERE kind='pending'" + scope + " ORDER BY expires LIMIT ?)", (*args, surplus))

    def approve(self, request_id: str, supplied_secret: str, target_name: str | None = None) -> str | Denied:
        """The redirect back to the client, or why there is none."""
        with self._approval_lock:
            return self._approve(request_id, supplied_secret, target_name)

    def _approve(self, request_id: str, supplied_secret: str, target_name: str | None) -> str | Denied:
        if not self.has_pending(request_id):
            return Denied(400, "This authorization request is unknown or has expired. "
                               "Start the connection again from your client.")
        # A resource that names its target locks it; the choice on the page
        # applies only to a resource that does not.
        target = self.targets.get(self.pending_target(request_id) or target_name or "")
        if target is None:
            return Denied(400, f"Choose {self.target_noun()}.")
        expected = target.secret_file.read_text().strip()
        if not expected or not hmac.compare_digest(supplied_secret.encode(), expected.encode()):
            return self._wrong_secret(request_id)
        self.wrong_secrets.pop(request_id, None)
        with self._transaction() as db:
            pending = self._get(db, "pending", request_id, Pending)
            if pending is None:  # expired between the check above and now
                return Denied(400, "This authorization request has expired. "
                                   "Start the connection again from your client.")
            params = pending.params
            code = AuthorizationCode(
                code=self._new_token("code_"), client_id=pending.client.client_id,
                scopes=params.scopes or list(self.policy.scopes), expires_at=time.time() + CODE_SECONDS,
                code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                resource=params.resource, subject=target.subject)
            self._put(db, "code", code.code, code)
            db.execute("DELETE FROM records WHERE kind='pending' AND key=?", (secret_key(request_id),))
            return construct_redirect_uri(str(params.redirect_uri), code=code.code, state=params.state)

    def target_noun(self) -> str:
        return "what to authorize"

    def _wrong_secret(self, request_id: str) -> Denied:
        """Count a wrong secret; the MAX_APPROVAL_ATTEMPTS-th cancels the request."""
        now = time.time()
        self.wrong_secrets = {key: value for key, value in self.wrong_secrets.items() if value[1] > now}
        count, expires = self.wrong_secrets.get(request_id, (0, now + PENDING_SECONDS))
        if count + 1 < MAX_APPROVAL_ATTEMPTS:
            self.wrong_secrets[request_id] = (count + 1, expires)
            return Denied(403, "Wrong approval secret.")
        self.wrong_secrets.pop(request_id, None)
        with self._transaction() as db:
            db.execute("DELETE FROM records WHERE kind='pending' AND key=?", (secret_key(request_id),))
        return Denied(403, f"Wrong approval secret, {MAX_APPROVAL_ATTEMPTS} times: this authorization request "
                           "is cancelled. Start the connection again from your client.")

    # Tokens.

    def _subject_allowed(self, grant: Any) -> bool:
        return grant is not None and grant.subject in self.subjects

    @_sdk_async
    def load_authorization_code(self, client: OAuthClientInformationFull,
                                authorization_code: str) -> AuthorizationCode | None:
        code = self._load("code", authorization_code, AuthorizationCode)
        return code if self._subject_allowed(code) and code.client_id == client.client_id else None

    @_sdk_async
    def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        token = self._load("refresh", refresh_token, RefreshToken)
        return token if self._subject_allowed(token) and token.client_id == client.client_id else None

    async def load_access_token(self, token: str) -> AccessToken | None:
        if self.also is not None and not token.startswith("access_"):
            found: AccessToken | None = await self.also.verify_token(token)
            return found
        grant: AccessToken | None = await asyncio.to_thread(self._load, "access", token, AccessToken)
        if grant is None or not self._subject_allowed(grant):
            return None
        if self.claims is not None:
            extra = self.claims(grant.subject or "")
            if extra is None:
                return None
            grant.claims = extra
        return grant

    def _issue(self, db: sqlite3.Connection, grant: Any, scopes: list[str]) -> OAuthToken:
        if not self._subject_allowed(grant):
            raise TokenError("invalid_grant", "This authorization is no longer configured")
        access_value, refresh_value = self._new_token("access_"), self._new_token("refresh_")
        pair = self._new_token("pair_")
        common = dict(client_id=grant.client_id, scopes=scopes, resource=grant.resource, subject=grant.subject)
        self._put(db, "access", access_value, AccessToken(
            token=access_value, expires_at=int(time.time()) + ACCESS_SECONDS, **common), pair)
        self._put(db, "refresh", refresh_value, RefreshToken(
            token=refresh_value, expires_at=int(time.time()) + REFRESH_SECONDS, **common), pair)
        db.execute("UPDATE clients SET last_used=? WHERE client_id=?", (time.time(), grant.client_id))
        return OAuthToken(access_token=access_value, token_type="Bearer", expires_in=ACCESS_SECONDS,
                          scope=" ".join(scopes), refresh_token=refresh_value)

    @_sdk_async
    def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                    authorization_code: AuthorizationCode) -> OAuthToken:
        with self._transaction() as db:
            stored = self._get(db, "code", authorization_code.code, AuthorizationCode)
            if stored is None or stored.client_id != client.client_id or stored != authorization_code:
                raise TokenError("invalid_grant", "Authorization code invalid or consumed")
            db.execute("DELETE FROM records WHERE kind='code' AND key=?", (secret_key(stored.code),))
            # The first completed authorization makes the client active (kept).
            # A metadata client past the cache cap is stored only now.
            now = time.time()
            origin = "metadata" if self.policy.metadata_allowed(client.client_id) else "dynamic"
            db.execute("INSERT INTO clients VALUES (?, ?, 'active', ?, ?, ?, ?, NULL) ON CONFLICT(client_id) "
                       "DO UPDATE SET state='active', expires=NULL, last_used=excluded.last_used",
                       (client.client_id, origin, client.model_dump_json(exclude_none=True), now, now, now))
            return self._issue(db, stored, stored.scopes)

    @_sdk_async
    def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
                               scopes: list[str]) -> OAuthToken:
        with self._transaction() as db:
            stored = self._get(db, "refresh", refresh_token.token, RefreshToken)
            if stored is None or stored.client_id != client.client_id or stored != refresh_token:
                raise TokenError("invalid_grant", "Refresh token invalid or consumed")
            granted = scopes or stored.scopes
            if not set(granted).issubset(stored.scopes):
                raise TokenError("invalid_scope", "Requested scope exceeds grant")
            db.execute("DELETE FROM records WHERE pair=(SELECT pair FROM records WHERE kind='refresh' AND key=?)",
                       (secret_key(stored.token),))
            return self._issue(db, stored, granted)

    @_sdk_async
    def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        kind = "refresh" if isinstance(token, RefreshToken) else "access"
        with self._transaction() as db:
            db.execute("DELETE FROM records WHERE pair=(SELECT pair FROM records WHERE kind=? AND key=?)",
                       (kind, secret_key(token.token)))

    def backup(self, destination: Path) -> None:
        """Consistent backup into a new private file; never overwrite a backup."""
        backup(self.database_file, destination)
