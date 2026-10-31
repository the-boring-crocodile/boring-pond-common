"""Revocation over the real SDK app, including authentication and request bounds."""
import base64
from urllib.parse import urlencode

import pytest

from boring_service_kit.issuer.clients import add_client, remove_client
from boring_service_kit.issuer.store import Transaction
from test_issuer import CALLBACK, CLAUDE, configured, run, sign_in


@pytest.mark.parametrize("origin", ["admin", "metadata"])
@pytest.mark.parametrize("hint", [None, "access_token", "refresh_token"])
def test_public_revocation_without_secret_invalidates_only_its_grant(tmp_path, origin, hint):
    config = configured(tmp_path)
    with Transaction(config.issuer.database_file) as db:
        client = add_client(db, "Public", [CALLBACK])["client_id"] if origin == "admin" else CLAUDE
        other = add_client(db, "Other public", [CALLBACK])["client_id"]

    async def scenario(service):
        _, _, response = await sign_in(service, client)
        tokens = response.json()
        _, _, response = await sign_in(service, other)
        other_tokens = response.json()
        token = tokens["access_token" if hint == "access_token" else "refresh_token"]
        form = {"client_id": client, "token": token}
        if hint is not None:
            form["token_type_hint"] = hint
        # A different client's request must not revoke this grant.
        assert (await service.client.post("/revoke", data={**form, "client_id": other})).status_code == 200
        assert (await service.call("whoami", {}, tokens["access_token"])).status_code == 200
        answer = await service.client.post("/revoke", data=form)
        assert answer.status_code == 200, answer.text
        assert (await service.call("whoami", {}, tokens["access_token"])).status_code == 401
        refresh = await service.client.post("/token", data={
            "grant_type": "refresh_token", "client_id": client, "refresh_token": tokens["refresh_token"]})
        assert refresh.status_code == 400 and refresh.json()["error"] == "invalid_grant"
        assert (await service.call("whoami", {}, other_tokens["access_token"])).status_code == 200
        assert (await service.client.post("/revoke", data=form)).status_code == 200
        assert (await service.client.post("/revoke", data={**form, "token": "unknown"})).status_code == 200
        if origin == "admin":
            with Transaction(config.issuer.database_file) as db:
                assert remove_client(db, client)
            assert (await service.client.post("/revoke", data=form)).status_code == 401
        metadata = (await service.client.get("/.well-known/oauth-authorization-server")).json()
        assert "none" in metadata["revocation_endpoint_auth_methods_supported"]
    run(config, scenario)


@pytest.mark.parametrize("auth", ["client_secret_basic", "client_secret_post"])
def test_revocation_keeps_confidential_client_authentication(tmp_path, auth):
    config = configured(tmp_path)
    with Transaction(config.issuer.database_file) as db:
        added = add_client(db, "Confidential", [CALLBACK], auth=auth)
    client, secret = added["client_id"], added["client_secret"]

    def basic(value):
        return {"Authorization": "Basic " + base64.b64encode(f"{client}:{value}".encode()).decode()}

    async def scenario(service):
        _, _, response = await sign_in(service, client,
            client_secret=secret if auth == "client_secret_post" else None,
            headers=basic(secret) if auth == "client_secret_basic" else None)
        assert response.status_code == 200, response.text
        tokens = response.json()
        form = {"client_id": client, "token": tokens["refresh_token"]}
        for wrong in (None, "", "wrong"):
            data, headers = dict(form), {}
            if wrong is not None:
                if auth == "client_secret_post":
                    data["client_secret"] = wrong
                else:
                    headers = basic(wrong)
            answer = await service.client.post("/revoke", data=data, headers=headers)
            assert answer.status_code == 401
            assert secret not in answer.text
            assert (await service.call("whoami", {}, tokens["access_token"])).status_code == 200
        data, headers = dict(form), {}
        if auth == "client_secret_post":
            # Encoded duplicate names retain the SDK parser\'s last-value rule.
            fields = [("client_id", client), ("token", "unknown"), ("token", tokens["refresh_token"])]
            def encode(items):
                return urlencode(items).replace("client_secret", "client%5Fsecret")
            headers = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
            wrong_last = encode([*fields, ("client_secret", secret), ("client_secret", "wrong")])
            assert (await service.client.post("/revoke", content=wrong_last, headers=headers)).status_code == 401
            assert (await service.call("whoami", {}, tokens["access_token"])).status_code == 200
            right_last = encode([*fields, ("client_secret", "wrong"), ("client_secret", secret)])
            answer = await service.client.post("/revoke", content=right_last, headers=headers)
            data["client_secret"] = secret
        else:
            headers = basic(secret)
            answer = await service.client.post("/revoke", data=data, headers=headers)
        assert answer.status_code == 200, answer.text
        assert (await service.call("whoami", {}, tokens["access_token"])).status_code == 401
        refreshed = await service.client.post("/token", data={
            "grant_type": "refresh_token", "client_id": client, "refresh_token": tokens["refresh_token"],
            **({"client_secret": secret} if auth == "client_secret_post" else {})},
            headers=basic(secret) if auth == "client_secret_basic" else None)
        assert refreshed.status_code == 400 and refreshed.json()["error"] == "invalid_grant"
        with Transaction(config.issuer.database_file) as db:
            assert remove_client(db, client)
        assert (await service.client.post("/revoke", data=data, headers=headers)).status_code == 401
    run(config, scenario)


def test_revocation_preserves_form_semantics_cors_and_body_limit(tmp_path):
    from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE
    config = configured(tmp_path)

    async def scenario(service):
        _, _, response = await sign_in(service)
        tokens = response.json()
        form = {"client_id": CLAUDE, "token": tokens["refresh_token"]}
        origin = {"Origin": "https://client.example"}
        preflight = await service.client.options("/revoke", headers={
            **origin, "Access-Control-Request-Method": "POST"})
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "*"
        for data in ({"client_id": CLAUDE}, {**form, "token_type_hint": "invalid"}):
            answer = await service.client.post("/revoke", data=data, headers=origin)
            assert answer.status_code == 400
            assert answer.headers["access-control-allow-origin"] == "*"
        assert (await service.client.post("/revoke", json=form)).status_code == 401
        assert (await service.call("whoami", {}, tokens["access_token"])).status_code == 200
        # Preserve duplicate fields and the SDK's last-value semantics.
        body = urlencode([("token", "unknown"), ("token", tokens["refresh_token"]),
                          ("client_id", "unknown"), ("client_id", CLAUDE)])
        answer = await service.client.post("/revoke", content=body,
            headers={**origin, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
        assert answer.status_code == 200, answer.text
        assert (await service.call("whoami", {}, tokens["access_token"])).status_code == 401
        # The original SDK limit applies to incoming bytes, before the adapter.
        boundary = urlencode({**form, "token": "unknown"}).encode()
        # Individual fields must also stay below the form parser\'s own limit.
        for index in range(5):
            key = f"&padding{index}=".encode()
            size = min(900000, DEFAULT_MAX_REQUEST_BODY_SIZE - len(boundary) - len(key))
            boundary += key + b"x" * size
        assert len(boundary) == DEFAULT_MAX_REQUEST_BODY_SIZE
        answer = await service.client.post("/revoke", content=boundary,
            headers={**origin, "Content-Type": "application/x-www-form-urlencoded"})
        assert answer.status_code == 200, answer.text
        answer = await service.client.post("/revoke", content=b"x" * (DEFAULT_MAX_REQUEST_BODY_SIZE + 1),
            headers={**origin, "Content-Type": "application/x-www-form-urlencoded"})
        assert answer.status_code == 413
        assert answer.headers["access-control-allow-origin"] == "*"
        async def chunks():
            for _ in range(3):
                yield b"x" * (DEFAULT_MAX_REQUEST_BODY_SIZE // 2 + 1)
        answer = await service.client.post("/revoke", content=chunks(),
            headers={**origin, "Content-Type": "application/x-www-form-urlencoded"})
        assert answer.status_code == 413
        assert answer.headers["access-control-allow-origin"] == "*"
    run(config, scenario)
