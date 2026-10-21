"""A server that is its own issuer: the whole sign-in over the real SDK app."""
import asyncio
import base64
import hashlib
import json
import sqlite3
import os
import stat
import time
from contextlib import closing
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.server.mcpserver import MCPServer

from boring_service_kit import keys
from boring_service_kit.access import calling_author, permitted
from boring_service_kit.calllog import CallLog
from boring_service_kit.http import ServiceConfig
from boring_service_kit.issuer import ClientPolicy
from boring_service_kit.issuer.clients import (ClientRejected, add_client, client_from_document, list_clients,
                                               prune_clients, remove_client)
from boring_service_kit.issuer.store import Transaction, create_tables, initialize_issuer_store
from boring_service_kit.testing import Service
from boring_service_kit.tools import READ_ONLY, as_mcp_tool

URL = "http://127.0.0.1:18599/mcp"
CLAUDE = "https://claude.ai/oauth/test-client.json"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
SECRET = "approval-secret-for-tests"


def document(client_id=CLAUDE, **changes):
    data = {"client_id": client_id, "client_name": "Claude", "redirect_uris": [CALLBACK],
            "token_endpoint_auth_methods_supported": ["none"], **changes}
    return json.dumps(data).encode()


def configured(tmp_path, **issuer):
    state = tmp_path / "state"
    state.mkdir(mode=0o700, exist_ok=True)
    (tmp_path / "approval").write_text(SECRET)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "url": URL,
        "issuer": {"database": "state/oauth.db", "approval_secret_file": "approval", "subject": "robin",
                   "name": "Robin", **issuer},
        "access": [{"subject": "robin", "role": "writer"}]}))
    config = ServiceConfig(path)
    fetched = []

    def fetch(url):
        fetched.append(url)
        return document(url)
    config.issuer.fetch = fetch
    config.fetched = fetched
    return config


def server_for(config):
    server = MCPServer("issuer-test", **config.auth)

    def whoami():
        return {"author": calling_author()}
    server.tool(annotations=READ_ONLY)(as_mcp_tool(permitted(whoami, "reader"), CallLog(None)))
    return server


def pkce():
    verifier = base64.urlsafe_b64encode(b"v" * 40).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


async def sign_in(service, client_id=CLAUDE, redirect=CALLBACK, secret=SECRET, client_secret=None):
    verifier, challenge = pkce()
    response = await service.client.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect, "code_challenge": challenge,
        "code_challenge_method": "S256", "state": "st", "resource": URL, "scope": "mcp"})
    assert response.status_code == 302, response.text
    approve = urlsplit(response.headers["location"])
    assert approve.path == "/approve"
    page = await service.client.get("/approve?" + approve.query)
    assert page.status_code == 200
    answer = await service.client.post("/approve?" + approve.query, data={"secret": secret})
    if answer.status_code != 302:
        return page.text, answer, None
    back = urlsplit(answer.headers["location"])
    code = parse_qs(back.query)["code"][0]
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect, "client_id": client_id,
            "code_verifier": verifier, "resource": URL}
    if client_secret:
        form["client_secret"] = client_secret
    token = await service.client.post("/token", data=form)
    return page.text, answer, token


def run(config, scenario):
    async def go():
        async with Service(config.app(server_for(config)), URL) as service:
            await scenario(service)
    asyncio.run(go())


def test_metadata_advertises_metadata_documents_and_no_registration(tmp_path):
    config = configured(tmp_path)

    async def scenario(service):
        metadata = (await service.client.get("/.well-known/oauth-authorization-server")).json()
        assert metadata["client_id_metadata_document_supported"] is True
        assert "none" in metadata["token_endpoint_auth_methods_supported"]
        assert metadata["code_challenge_methods_supported"] == ["S256"]
        assert "registration_endpoint" not in metadata
        assert (await service.client.post("/register", json={"redirect_uris": [CALLBACK]})).status_code == 404
        resource = (await service.client.get("/.well-known/oauth-protected-resource/mcp")).json()
        assert resource["authorization_servers"] == [metadata["issuer"]]
        unauthenticated = await service.call("whoami", {}, token=None)
        assert unauthenticated.status_code == 401
        assert "resource_metadata=" in unauthenticated.headers["www-authenticate"]
    run(config, scenario)


def test_metadata_document_client_signs_in_uses_tools_and_refreshes(tmp_path):
    config = configured(tmp_path)

    async def scenario(service):
        page, _, token = await sign_in(service)
        assert '<p class="to">claude.ai</p>' in page and "published document on claude.ai" in page
        assert token.status_code == 200, token.text
        tokens = token.json()
        assert await service.result("whoami", {}, tokens["access_token"]) == (False, {"author": '["http://127.0.0.1:18599","robin"]'})
        refreshed = await service.client.post("/token", data={
            "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": CLAUDE})
        assert refreshed.status_code == 200
        again = await service.client.post("/token", data={
            "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": CLAUDE})
        assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
        assert (await service.call("whoami", {}, tokens["access_token"])).status_code == 401  # rotated away
    run(config, scenario)
    with Transaction(config.issuer.database_file) as db:
        [client] = list_clients(db)
    assert client["origin"] == "metadata" and client["state"] == "active" and client["expires"] is None
    assert client["refresh_tokens"] == 1 and client["redirect_hosts"] == ["claude.ai"]
    assert config.fetched == [CLAUDE]  # one fetch, then the cache


def test_wrong_secret_five_times_cancels_but_never_locks_out(tmp_path):
    config = configured(tmp_path)

    async def scenario(service):
        _, answer, _ = await sign_in(service, secret="wrong")
        assert answer.status_code == 403
        _, _, token = await sign_in(service)  # a new request with the right secret still works
        assert token.status_code == 200
    run(config, scenario)


def test_fifth_wrong_secret_cancels_that_request_only(tmp_path):
    config = configured(tmp_path)

    def pending_rows():
        with closing(sqlite3.connect(config.issuer.database_file)) as db:
            return db.execute("SELECT COUNT(*) FROM records WHERE kind='pending'").fetchone()[0]

    async def scenario(service):
        verifier, challenge = pkce()
        response = await service.client.get("/authorize", params={
            "response_type": "code", "client_id": CLAUDE, "redirect_uri": CALLBACK, "code_challenge": challenge,
            "code_challenge_method": "S256", "state": "st", "resource": URL, "scope": "mcp"})
        assert response.status_code == 302, response.text
        approve = "/approve?" + urlsplit(response.headers["location"]).query
        assert pending_rows() == 1
        for attempt in range(5):
            answer = await service.client.post(approve, data={"secret": "wrong"})
            assert answer.status_code == 403, (attempt, answer.text)
        assert "cancelled" in answer.text
        assert pending_rows() == 0
        right = await service.client.post(approve, data={"secret": SECRET})
        assert right.status_code == 400  # the cancelled request stays cancelled
        assert "location" not in right.headers
        _, _, token = await sign_in(service)  # a new request with the right secret still works
        assert token.status_code == 200
    run(config, scenario)


async def open_request(service, client_id=CLAUDE):
    """/authorize only, as a stranger can send it: the response."""
    verifier, challenge = pkce()
    return await service.client.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK, "code_challenge": challenge,
        "code_challenge_method": "S256", "state": "st", "resource": URL, "scope": "mcp"})


def test_strangers_open_requests_do_not_lock_the_owner_out(tmp_path):
    from boring_service_kit.issuer.provider import MAX_PENDING_PER_CLIENT
    config = configured(tmp_path)

    def pending_rows():
        with closing(sqlite3.connect(config.issuer.database_file)) as db:
            return db.execute("SELECT COUNT(*) FROM records WHERE kind='pending'").fetchone()[0]

    async def scenario(service):
        stranger = []
        for _ in range(MAX_PENDING_PER_CLIENT + 2):
            response = await open_request(service)
            assert response.status_code == 302, response.text
            stranger.append("/approve?" + urlsplit(response.headers["location"]).query)
        assert pending_rows() == MAX_PENDING_PER_CLIENT
        assert (await service.client.get(stranger[0])).status_code == 400  # the oldest made room
        assert (await service.client.get(stranger[-1])).status_code == 200
        _, _, token = await sign_in(service)  # the owner, with the same public client id
        assert token.status_code == 200, token.text
    run(config, scenario)


def test_failed_metadata_document_is_not_fetched_again_at_once(tmp_path, monkeypatch):
    from boring_service_kit.issuer import provider
    config = configured(tmp_path)
    fetched = []

    def fetch(url):
        fetched.append(url)
        raise ClientRejected("metadata document answered HTTP 404")
    config.issuer.fetch = fetch
    bogus = [f"https://claude.ai/bogus/{n}.json" for n in range(4)]

    async def scenario(service):
        for _ in range(3):
            assert (await open_request(service, bogus[0])).status_code == 400
        assert fetched == [bogus[0]]  # remembered: one fetch, not one per request
        monkeypatch.setattr(provider, "MAX_FAILED_DOCUMENTS", 2)
        for client_id in bogus[1:3]:
            assert (await open_request(service, client_id)).status_code == 400
        assert list(config.issuer.failed_documents) == bogus[1:3]  # bounded: the oldest made room
        assert (await open_request(service, bogus[0])).status_code == 400
        assert fetched == [bogus[0], bogus[1], bogus[2], bogus[0]]
        monkeypatch.setattr(provider, "FAILED_DOCUMENT_SECONDS", 0)
        for _ in range(2):
            assert (await open_request(service, bogus[3])).status_code == 400
        assert fetched[-2:] == [bogus[3], bogus[3]]  # remembered for no time: fetched each time
    run(config, scenario)


@pytest.mark.parametrize("client_id", ["https://evil.example/claude.json", "http://claude.ai/x.json",
                                       "https://claude.ai/", "https://claude.ai:8443/x.json"])
def test_documents_elsewhere_are_never_fetched(tmp_path, client_id):
    config = configured(tmp_path)

    async def scenario(service):
        verifier, challenge = pkce()
        response = await service.client.get("/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK, "code_challenge": challenge,
            "code_challenge_method": "S256", "state": "st", "resource": URL})
        assert response.status_code == 400
    run(config, scenario)
    assert config.fetched == []


def test_document_checks():
    scopes = ("mcp",)
    assert client_from_document(CLAUDE, document(), scopes).token_endpoint_auth_method == "none"
    chatgpt = document(token_endpoint_auth_method="private_key_jwt",
                       token_endpoint_auth_methods_supported=["none", "private_key_jwt"])
    assert client_from_document(CLAUDE, chatgpt, scopes).scope == "mcp"
    for bad in [document(client_id="https://claude.ai/other.json"), document(redirect_uris=[]),
                document(client_secret="x"), b"[]", b"not json",
                document(token_endpoint_auth_method="private_key_jwt", token_endpoint_auth_methods_supported=None)]:
        with pytest.raises(ClientRejected):
            client_from_document(CLAUDE, bad, scopes)
    policy = ClientPolicy(metadata_hosts=("claude.ai", ".example.org"))
    assert policy.metadata_allowed("https://a.example.org/c.json") and not policy.metadata_allowed("https://example.org.evil/c")
    with pytest.raises(ValueError):
        ClientPolicy(metadata_hosts=("Claude.ai",))


def test_admin_client_with_loopback_redirect_on_any_port(tmp_path):
    config = configured(tmp_path)
    with Transaction(config.issuer.database_file) as db:
        added = add_client(db, "VS Code", ["http://127.0.0.1/callback"])
    assert added["client_secret"] is None

    async def scenario(service):
        page, _, token = await sign_in(service, added["client_id"], "http://127.0.0.1:43123/callback")
        assert "added by this server's administrator" in page and "Any local program" in page
        assert token.status_code == 200, token.text
        verifier, challenge = pkce()
        other = await service.client.get("/authorize", params={
            "response_type": "code", "client_id": added["client_id"], "redirect_uri": "http://127.0.0.1:5/elsewhere",
            "code_challenge": challenge, "code_challenge_method": "S256", "resource": URL})
        assert other.status_code == 400
    run(config, scenario)


def test_approval_page_lets_the_form_redirect_to_the_callback():
    from boring_service_kit.issuer.web import CONSENT_HEADERS, consent_headers
    # Chromium applies form-action to the redirect after the post: the callback's origin must be named.
    assert "form-action 'self' http://127.0.0.1:55879;" in consent_headers("http://127.0.0.1:55879/")[
        "Content-Security-Policy"]
    assert "form-action 'self' https://claude.ai;" in consent_headers(CALLBACK)["Content-Security-Policy"]
    assert "form-action 'self' vscode://callback;" in consent_headers("vscode://callback")["Content-Security-Policy"]
    assert consent_headers("https://evil.example;script-src *") == CONSENT_HEADERS
    assert consent_headers(None) == CONSENT_HEADERS


def test_admin_client_with_secret(tmp_path):
    config = configured(tmp_path)
    with Transaction(config.issuer.database_file) as db:
        added = add_client(db, "Copilot Studio", ["https://copilot.example/callback"], auth="client_secret_post")

    async def scenario(service):
        _, _, token = await sign_in(service, added["client_id"], "https://copilot.example/callback")
        assert token.status_code == 401  # no secret
        _, _, token = await sign_in(service, added["client_id"], "https://copilot.example/callback",
                                    client_secret=added["client_secret"])
        assert token.status_code == 200
    run(config, scenario)
    with pytest.raises(ValueError, match="HTTPS"):
        with Transaction(config.issuer.database_file) as db:
            add_client(db, "plain", ["http://example.com/cb"])


def test_dynamic_registration_only_when_enabled(tmp_path):
    config = configured(tmp_path, dynamic_registration=True)

    async def scenario(service):
        assert "registration_endpoint" in (await service.client.get("/.well-known/oauth-authorization-server")).json()
        registered = await service.client.post("/register", json={
            "redirect_uris": ["https://attacker.example/cb"], "client_name": "Claude",
            "token_endpoint_auth_method": "none"})
        assert registered.status_code == 201, registered.text
        client_id = registered.json()["client_id"]
        with Transaction(config.issuer.database_file) as db:
            [row] = list_clients(db)
        assert row["origin"] == "dynamic" and row["state"] == "registered" and row["expires"] > time.time()
        page, _, token = await sign_in(service, client_id, "https://attacker.example/cb")
        assert '<p class="to">attacker.example</p>' in page and "registered itself" in page
        assert token.status_code == 200
    run(config, scenario)


def test_access_keys_work_beside_the_issuer(tmp_path):
    keyfile = tmp_path / "keys.json"
    _, key = keys.add(keyfile, "script@host", "reader", 1, issuer="key:issuer-test")
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    (tmp_path / "approval").write_text(SECRET)
    (tmp_path / "config.json").write_text(json.dumps({
        "url": URL, "keys": {"file": "keys.json"},
        "issuer": {"database": "state/oauth.db", "approval_secret_file": "approval", "subject": "robin"},
        "access": [{"subject": "robin", "role": "writer"}]}))
    config = ServiceConfig(tmp_path / "config.json")

    async def scenario(service):
        refused, data = await service.result("whoami", {}, key)
        assert not refused and "script@host" in data["author"]
        assert (await service.call("whoami", {}, "access_not-a-real-token")).status_code == 401
    run(config, scenario)


def test_config_rules_for_the_issuer(tmp_path):
    (tmp_path / "secret").write_text("x")
    base = {"url": URL, "issuer": {"database": "oauth.db", "approval_secret_file": "secret", "subject": "robin"}}

    def load(data):
        (tmp_path / "config.json").write_text(json.dumps(data))
        return ServiceConfig(tmp_path / "config.json")
    with pytest.raises(ValueError, match="listed in access"):
        load({**base, "access": []})
    with pytest.raises(ValueError, match="exclude each other"):
        load({**base, "oauth": {}, "access": []})
    with pytest.raises(ValueError, match="issuer supports"):
        load({**base, "issuer": {**base["issuer"], "registration": True}, "access": []})


def test_remove_and_prune(tmp_path):
    config = configured(tmp_path)

    async def scenario(service):
        _, _, token = await sign_in(service)
        assert token.status_code == 200
        service.token = token.json()["access_token"]
        with Transaction(config.issuer.database_file) as db:
            assert prune_clients(db, idle_days=0) == []  # it holds a live refresh token
            assert remove_client(db, CLAUDE)
        assert (await service.call("whoami", {}, service.token)).status_code == 401
        with closing(sqlite3.connect(config.issuer.database_file)) as db:
            assert list_clients(db) == []
            assert db.execute("SELECT COUNT(*) FROM records WHERE kind IN ('access', 'refresh', 'code')"
                              ).fetchone()[0] == 0
    run(config, scenario)
    with Transaction(config.issuer.database_file) as db:
        db.execute("INSERT INTO clients VALUES ('old', 'dynamic', 'active', '{}', 0, 0, 0, NULL)")
        assert [c["client_id"] for c in prune_clients(db, idle_days=30, apply=True)] == ["old"]
        assert list_clients(db) == []


def test_removed_client_refresh_token_no_longer_works(tmp_path):
    config = configured(tmp_path)

    async def scenario(service):
        _, _, token = await sign_in(service)
        assert token.status_code == 200
        tokens = token.json()
        with Transaction(config.issuer.database_file) as db:
            assert remove_client(db, CLAUDE)
        refreshed = await service.client.post("/token", data={
            "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": CLAUDE})
        assert refreshed.status_code == 400, refreshed.text
        assert refreshed.json()["error"] == "invalid_grant"
        assert (await service.call("whoami", {}, tokens["access_token"])).status_code == 401
    run(config, scenario)


def test_unused_clients_expire(tmp_path):
    config = configured(tmp_path)

    async def scenario(service):
        verifier, challenge = pkce()
        await service.client.get("/authorize", params={
            "response_type": "code", "client_id": CLAUDE, "redirect_uri": CALLBACK, "code_challenge": challenge,
            "code_challenge_method": "S256", "resource": URL})
    run(config, scenario)
    with Transaction(config.issuer.database_file) as db:
        [row] = list_clients(db)
        assert row["state"] == "registered"
        db.execute("UPDATE clients SET expires=1")
    with config.issuer._transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 0


def test_store_refuses_foreign_files(tmp_path):
    db = sqlite3.connect(tmp_path / "x.db", isolation_level=None)
    db.execute("CREATE TABLE other (x)")
    with pytest.raises(ValueError, match="unversioned"):
        initialize_issuer_store(db)


def test_admin_command(tmp_path, capsys):
    from boring_service_kit.issuer.cli import main
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    store = str(state / "oauth.db")
    main(["--db", store, "client-add", "--name", "VS Code", "--redirect-uri", "http://127.0.0.1/"])
    added = json.loads(capsys.readouterr().out)
    assert added["client_id"].startswith("cli_") and added["client_secret"] is None
    main(["--db", store, "clients"])
    assert [c["name"] for c in json.loads(capsys.readouterr().out)] == ["VS Code"]
    main(["--db", store, "backup", str(state / "copy.db")])
    assert json.loads(capsys.readouterr().out)["backup"].endswith("copy.db")
    copy = state / "copy.db"
    assert copy.is_file()
    if os.name == "posix":  # Windows has no mode bits; the store's folder ACL is what protects it there
        assert stat.S_IMODE(copy.stat().st_mode) == 0o600
    with closing(sqlite3.connect(copy)) as db:
        assert [(c["client_id"], c["name"]) for c in list_clients(db)] == [(added["client_id"], "VS Code")]
    copied = copy.read_bytes()
    with pytest.raises(SystemExit):
        main(["--db", store, "backup", str(copy)])  # never overwrite a backup
    capsys.readouterr()
    assert copy.read_bytes() == copied
    main(["--db", store, "client-remove", added["client_id"]])
    capsys.readouterr()
    with closing(sqlite3.connect(store)) as db:
        assert list_clients(db) == []
        assert db.execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 0
    with pytest.raises(SystemExit):
        main(["--db", store, "client-remove", added["client_id"]])


def test_issuer_under_a_path_behind_a_path_keeping_proxy(tmp_path):
    """A site may serve services under /services/<name>/: the issuer lives there too."""
    from boring_service_kit.testing import metadata_document, sign_in
    url = "http://127.0.0.1:18599/services/lone/mcp"
    (tmp_path / "state").mkdir(mode=0o700)
    (tmp_path / "approval").write_text(SECRET)
    (tmp_path / "config.json").write_text(json.dumps({
        "url": url, "issuer": {"url": "http://127.0.0.1:18599/services/lone", "database": "state/oauth.db",
                               "approval_secret_file": "approval", "subject": "robin"},
        "access": [{"subject": "robin", "role": "writer"}]}))
    config = ServiceConfig(tmp_path / "config.json")
    config.issuer.fetch = lambda client_id: metadata_document(client_id, CALLBACK)
    assert config.issuer_paths[0] == "/services/lone/authorize"

    async def scenario(service):
        metadata = (await service.client.get("/.well-known/oauth-authorization-server/services/lone")).json()
        assert metadata["issuer"].rstrip("/") == "http://127.0.0.1:18599/services/lone"
        assert metadata["authorization_endpoint"] == "http://127.0.0.1:18599/services/lone/authorize"
        resource = (await service.client.get("/.well-known/oauth-protected-resource/services/lone/mcp")).json()
        assert resource["authorization_servers"] == [metadata["issuer"]]
        tokens = await sign_in(service.client, url, CLAUDE, CALLBACK, SECRET, issuer_path="/services/lone")
        from boring_service_kit.testing import mcp_headers, mcp_meta
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "whoami", "arguments": {}, "_meta": mcp_meta()}}
        called = await service.client.post("/services/lone/mcp", json=body,
                                           headers=mcp_headers("tools/call", "whoami", tokens["access_token"]))
        assert called.status_code == 200 and "robin" in called.json()["result"]["content"][0]["text"]

    async def go():
        async with Service(config.app(server_for(config)), url) as service:
            service.client.base_url = "http://127.0.0.1:18599"
            await scenario(service)
    asyncio.run(go())
