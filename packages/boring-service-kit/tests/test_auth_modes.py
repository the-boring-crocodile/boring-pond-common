"""A service has one auth owner; role updates cross the existing cache boundary."""
import asyncio
import json
import time

import pytest

from boring_service_kit.http import ServiceConfig
from boring_service_kit.testing import FakeControlPlane
from boring_service_kit import keys


def config(tmp_path, mode, **block):
    value = {"url": "http://127.0.0.1:18599/mcp", "mode": mode, mode: block}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value))
    return path


def crocodile(tmp_path, authority):
    (tmp_path / "secret").write_text("test-only")
    return config(tmp_path, "crocodile", url=authority.url, client_id="test-client", secret_file="secret")


def test_crocodile_uses_one_authority_and_current_introspection_roles(tmp_path):
    async def scenario():
        cp = FakeControlPlane()
        try:
            instance = ServiceConfig(crocodile(tmp_path, cp))
            assert instance.watchdog is not None
            assert instance.call_limit == 28.0
            verifier = instance.token_verifier
            now = [time.time()]
            verifier.clock = lambda: now[0]
            cp.tokens["same-bearer"] = {"active": True, "aud": instance.url, "sub": "pirate/parrot",
                                        "role": "writer", "exp": int(now[0]) + 600}
            first = await verifier.verify_token("same-bearer")
            assert first.claims["boring_caller"]["role"] == "writer"
            assert json.loads(first.claims["boring_caller"]["author"]) == [cp.url, "pirate/parrot"]
            cp.tokens["same-bearer"]["role"] = "reader"
            now[0] += 59
            assert (await verifier.verify_token("same-bearer")).claims["boring_caller"]["role"] == "writer"
            now[0] += 1
            assert (await verifier.verify_token("same-bearer")).claims["boring_caller"]["role"] == "reader"
            # A newly admitted subject has no local access list to update.
            cp.tokens["new-caller"] = {**cp.tokens["same-bearer"], "sub": "new-pond"}
            assert (await verifier.verify_token("new-caller")).claims["boring_caller"]["subject"] == "new-pond"
            cp.tokens["no-role"] = {k: v for k, v in cp.tokens["same-bearer"].items() if k != "role"}
            assert await verifier.verify_token("no-role") is None
        finally:
            cp.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("other", ["keys", "issuer"])
def test_modes_cannot_be_combined(tmp_path, other):
    path = config(tmp_path, "crocodile", url="https://authority.test", client_id="id", secret_file="absent")
    value = json.loads(path.read_text())
    value[other] = {}
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="exactly one"):
        ServiceConfig(path)


@pytest.mark.parametrize("field,replacement", [
    ("access", "role"), ("oauth", "crocodile"), ("pond", "crocodile"),
])
def test_superseded_auth_fields_name_the_replacement(tmp_path, field, replacement):
    path = config(tmp_path, "keys", file="keys.json")
    value = json.loads(path.read_text())
    value[field] = {}
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match=replacement):
        ServiceConfig(path)


def test_keys_mode_uses_the_role_on_the_key(tmp_path):
    _, bearer = keys.add(tmp_path / "keys.json", "robin", "reader", 1, issuer="key:standalone")
    instance = ServiceConfig(config(tmp_path, "keys", file="keys.json"))
    token = asyncio.run(instance.token_verifier.verify_token(bearer))
    assert token.claims["boring_caller"]["role"] == "reader"
    assert instance.watchdog is None


def test_own_issuer_needs_one_role_before_opening_state(tmp_path):
    path = config(tmp_path, "issuer", subject="owner", database="state/issuer.db",
                  approval_secret_file="approval")
    with pytest.raises(ValueError, match="role"):
        ServiceConfig.validate(path)
    value = json.loads(path.read_text())
    value["issuer"]["role"] = "writer"
    path.write_text(json.dumps(value))
    ServiceConfig.validate(path)
    assert not (tmp_path / "state").exists()
