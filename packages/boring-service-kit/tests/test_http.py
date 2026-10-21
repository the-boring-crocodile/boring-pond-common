"""Independent HTTP: real MCP app, fake authenticated introspection authority."""
import asyncio
import json
import time
import pytest
from mcp.server.mcpserver import MCPServer
from boring_service_kit.access import AccessPolicy, calling_author, permitted
from boring_service_kit.calllog import CallLog
from boring_service_kit.http import ServiceConfig
from boring_service_kit.pond import wrap_app
from boring_service_kit.tools import READ_ONLY, WRITES, as_mcp_tool
from boring_service_kit.testing import FakeControlPlane, Service
from boring_service_kit.testing import mcp_headers as _headers, mcp_meta as _meta


def configured(tmp_path, cp, access=None):
    (tmp_path / "secret").write_text("test-only")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "url": "http://127.0.0.1:18599/mcp",
        "oauth": {"issuer": cp.url, "introspection_url": cp.url + "/introspect",
                  "client_id": "test-client", "secret_file": "secret"},
        "access": access if access is not None else [
            {"subject": "pond", "role": "writer", "author": "pond"},
            {"subject": "alice", "role": "reader"}]}))
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
        assert ServiceConfig(path).bind == "0.0.0.0"
    finally:
        cp.close()


def test_call_limit_only_behind_a_pond(tmp_path):
    cp = FakeControlPlane()
    try:
        config = configured(tmp_path, cp)
        assert config.call_limit is None  # no pond section: Copilot on loopback waits
        path = tmp_path / "config.json"
        data = json.loads(path.read_text())
        data["pond"] = {"url": cp.url, "client_id": "test-client", "secret_file": "secret"}
        path.write_text(json.dumps(data))
        assert ServiceConfig(path).call_limit == 28.0  # the Pond's 30 s less the answer's 2
    finally:
        cp.close()


def test_config_default_denies_and_aliases_cannot_collide(tmp_path):
    cp = FakeControlPlane()
    try:
        config = configured(tmp_path, cp, [])
        assert config.watchdog is None
        assert config.verifier.accept("t", {"active": True, "sub": "pond",
            "aud": config.url, "exp": int(time.time()) + 60}, time.time()) is None
        with pytest.raises(ValueError, match="unique"):
            AccessPolicy(cp.url, [{"subject": "pond", "role": "writer", "author": "same"},
                                 {"subject": "alice", "role": "writer", "author": "same"}])
    finally:
        cp.close()


def test_reader_writer_identity_and_no_unauthenticated_standalone_discover(tmp_path):
    effects = []
    async def scenario():
        service = Service()
        config = configured(tmp_path, service.cp)
        good = {"active": True, "aud": config.url, "exp": int(time.time()) + 60}
        service.cp.tokens = {
            "writer": {**good, "sub": "pond"},
            "reader": {**good, "sub": "alice"},
            "unknown": {**good, "sub": "mallory"},
            "issuer": {**good, "sub": "pond", "iss": "https://other.invalid"},
            "audience": {**good, "sub": "pond", "aud": "https://other.invalid/mcp"}}
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
            assert await service.result("write", {}, "writer") == (False, {"author": "pond"})
            refused, data = await service.result("write", {}, "reader")
            assert refused and data["code"] == "forbidden" and effects == ["pond"]
            refused, data = await service.result("read", {}, "reader")
            assert not refused and json.loads(data["author"]) == [service.cp.url, "alice"]
            for token in [None, "unknown", "issuer", "audience"]:
                assert (await service.call("read", {}, token)).status_code == 401
    asyncio.run(scenario())


def test_bad_introspection_issuer_and_scopes_are_rejected(tmp_path):
    cp = FakeControlPlane()
    try:
        config = configured(tmp_path, cp)
        verifier = config.verifier
        answer = {"active": True, "aud": [config.url], "sub": "pond", "exp": int(time.time()) + 60}
        assert verifier.accept("t", answer, time.time()) is not None
        assert verifier.accept("t", {**answer, "iss": None}, time.time()) is None
        verifier.scopes = ["read"]
        assert verifier.accept("t", answer, time.time()) is None
        assert verifier.accept("t", {**answer, "scope": "read write"}, time.time()) is not None
    finally:
        cp.close()
