"""Independent HTTP: real MCP app, fake authenticated introspection authority."""
import asyncio
import json
import time
import pytest
from mcp.server.mcpserver import MCPServer
from boring_service_kit.access import calling_author, permitted
from boring_service_kit.calllog import CallLog
from boring_service_kit.http import ServiceConfig
from boring_service_kit.pond import wrap_app
from boring_service_kit.tools import READ_ONLY, WRITES, as_mcp_tool
from boring_service_kit.testing import FakeControlPlane, Service
from boring_service_kit.testing import mcp_headers as _headers, mcp_meta as _meta


def configured(tmp_path, cp):
    (tmp_path / "secret").write_text("test-only")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "url": "http://127.0.0.1:18599/mcp", "mode": "crocodile",
        "crocodile": {"url": cp.url, "client_id": "test-client", "secret_file": "secret"}}))
    return ServiceConfig(path)


def test_oauth_cleartext_requires_loopback_bind_but_tls_proxy_is_supported(tmp_path):
    cp = FakeControlPlane()
    try:
        configured(tmp_path, cp)
        path = tmp_path / "config.json"
        data = json.loads(path.read_text())
        data["bind"] = "0.0.0.0"
        path.write_text(json.dumps(data))
        with pytest.raises(ValueError, match="loopback bind"):
            ServiceConfig(path)
        data["url"] = "https://service.test/mcp"
        path.write_text(json.dumps(data))
        with pytest.raises(ValueError, match="behind_proxy=true"):
            ServiceConfig(path)
        data["behind_proxy"] = True
        path.write_text(json.dumps(data))
        assert ServiceConfig(path).bind == "0.0.0.0"
    finally:
        cp.close()


@pytest.mark.parametrize("bind", ["0.0.0.0", "::", "192.0.2.1", "service.lan"])
@pytest.mark.parametrize("url", ["http://127.0.0.1:18599/mcp", "https://public.test/mcp"])
def test_access_key_guard_uses_bind_even_when_public_url_is_https(tmp_path, bind, url):
    path = tmp_path / "config.json"
    data = {"url": url, "mode": "keys", "bind": bind, "keys": {"file": "keys.json"}}
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="loopback bind"):
        ServiceConfig(path)
    data["behind_proxy"] = True
    path.write_text(json.dumps(data))
    assert ServiceConfig(path).behind_proxy is True


@pytest.mark.parametrize("bind", ["127.0.0.1", "127.2.3.4", "::1", "localhost"])
def test_loopback_needs_no_proxy_acknowledgement(tmp_path, bind):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"url": "https://public.test/mcp", "mode": "keys", "bind": bind,
                                "keys": {"file": "keys.json"}}))
    assert ServiceConfig(path).behind_proxy is False


@pytest.mark.parametrize("value", ["true", 1, None, []])
def test_proxy_acknowledgement_is_strictly_boolean(tmp_path, value):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"url": "https://public.test/mcp", "mode": "keys", "keys": {"file": "keys.json"},
                                "behind_proxy": value}))
    with pytest.raises(ValueError, match="behind_proxy must be boolean"):
        ServiceConfig(path)


def test_call_limit_is_owned_by_crocodile_mode(tmp_path):
    cp = FakeControlPlane()
    try:
        assert configured(tmp_path, cp).call_limit == 28.0
    finally:
        cp.close()
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"url": "https://public.test/mcp", "mode": "keys", "keys": {"file": "keys.json"}}))
    assert ServiceConfig(path).call_limit is None


def test_missing_introspection_role_is_refused(tmp_path):
    cp = FakeControlPlane()
    try:
        config = configured(tmp_path, cp)
        assert config.verifier.accept("t", {"active": True, "sub": "pond",
            "aud": config.url, "exp": int(time.time()) + 60}, time.time()) is None
    finally:
        cp.close()


def test_reader_writer_identity_and_no_unauthenticated_standalone_discover(tmp_path):
    effects = []
    async def scenario():
        service = Service()
        config = configured(tmp_path, service.cp)
        good = {"active": True, "aud": config.url, "exp": int(time.time()) + 60}
        service.cp.tokens = {
            "writer": {**good, "sub": "pond", "role": "writer"},
            "reader": {**good, "sub": "alice", "role": "reader"},
            "unknown": {**good, "sub": "mallory"},
            "issuer": {**good, "sub": "pond", "role": "writer", "iss": "https://other.invalid"},
            "audience": {**good, "sub": "pond", "role": "writer", "aud": "https://other.invalid/mcp"}}
        server = MCPServer("independent-test", **config.auth)
        def read():
            return {"author": calling_author(), "effects": list(effects)}
        def write():
            effects.append(calling_author())
            return {"author": calling_author()}
        for fn, role, hint in [(read, "reader", READ_ONLY), (write, "writer", WRITES)]:
            server.tool(annotations=hint)(as_mcp_tool(permitted(fn, role), CallLog(None)))
        service.app = wrap_app(server, config.url, discover_bypass=False)
        async with service:
            discover = {"jsonrpc": "2.0", "id": 1, "method": "server/discover",
                        "params": {"_meta": _meta()}}
            response = await service.client.post("/mcp", json=discover, headers=_headers("server/discover"))
            assert response.status_code == 401
            assert await service.result("write", {}, "writer") == (False, {"author": json.dumps([service.cp.url, "pond"], separators=(",", ":"))})
            refused, data = await service.result("write", {}, "reader")
            assert refused and data["code"] == "forbidden" and effects == [json.dumps([service.cp.url, "pond"], separators=(",", ":"))]
            refused, data = await service.result("read", {}, "reader")
            assert not refused and json.loads(data["author"]) == [service.cp.url, "alice"]
            for token in [None, "unknown", "issuer", "audience"]:
                assert (await service.call("read", {}, token)).status_code == 401
    asyncio.run(scenario())


def test_bad_introspection_issuer_and_roles_are_rejected(tmp_path):
    cp = FakeControlPlane()
    try:
        config = configured(tmp_path, cp)
        verifier = config.verifier
        answer = {"active": True, "aud": [config.url], "sub": "pond", "role": "writer", "exp": int(time.time()) + 60}
        assert verifier.accept("t", answer, time.time()) is not None
        assert verifier.accept("t", {**answer, "iss": None}, time.time()) is None
        assert verifier.accept("t", {**answer, "role": "administrator"}, time.time()) is None
        assert verifier.accept("t", {**answer, "role": "reader"}, time.time()) is not None
    finally:
        cp.close()
