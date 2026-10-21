"""Access keys: key file administration, verification, HTTP end to end."""
import asyncio
import json
import os
import stat
import time

import pytest
from mcp.server.mcpserver import MCPServer

from boring_service_kit import keys
from boring_service_kit.access import calling_author, permitted
from boring_service_kit.calllog import CallLog
from boring_service_kit.http import ServiceConfig
from boring_service_kit.pond import wrap_app
from boring_service_kit.tools import READ_ONLY, WRITES, as_mcp_tool
from boring_service_kit.testing import Service

ISSUER = "key:knowledge-test"
URL = "http://127.0.0.1:18599/mcp"


def test_add_stores_only_the_fingerprint(tmp_path):
    path = tmp_path / "keys.json"
    key, secret = keys.add(path, "robin@vm", "writer", 90, "Robin, VS", issuer=ISSUER, now=1_800_000_000)
    text = path.read_text()
    assert secret.startswith(keys.PREFIX) and len(secret) > 40
    assert secret not in text and keys.fingerprint(secret) in text
    loaded = keys.KeyFile.load(path)
    assert loaded.issuer == ISSUER and loaded.keys == (key,)
    assert key.expires == 1_800_000_000 + 90 * keys.DAY
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".keys-*"))  # no temporary file left


def test_new_file_needs_an_issuer_and_keeps_it(tmp_path):
    path = tmp_path / "keys.json"
    with pytest.raises(SystemExit, match="--issuer"):
        keys.add(path, "a", "reader", 1)
    with pytest.raises(SystemExit, match="key:<instance>"):
        keys.add(path, "a", "reader", 1, issuer="knowledge")
    keys.add(path, "a", "reader", 1, issuer=ISSUER)
    with pytest.raises(SystemExit, match="belongs to"):
        keys.add(path, "b", "reader", 1, issuer="key:other")
    keys.add(path, "b", "reader", 1)  # the file's issuer
    assert [k.subject for k in keys.KeyFile.load(path).keys] == ["a", "b"]


def verifier(path, clock=time.time):
    return keys.KeyVerifier(path, URL, ["mcp"], clock=clock)


def test_verifier_accepts_valid_keys_only(tmp_path):
    path = tmp_path / "keys.json"
    writer, secret = keys.add(path, "robin@vm", "writer", 1, issuer=ISSUER)
    v = verifier(path)
    access = asyncio.run(v.verify_token(secret))
    assert access.subject == "robin@vm" and access.resource == URL and access.scopes == ["mcp"]
    assert access.claims["boring_caller"] == {"issuer": ISSUER, "subject": "robin@vm", "role": "writer",
                                              "author": json.dumps([ISSUER, "robin@vm"], separators=(",", ":"))}
    for bad in ("", "pond", secret + "x", keys.PREFIX + "unknown", secret.upper()):
        assert asyncio.run(v.verify_token(bad)) is None


def test_expiry_renew_and_remove_take_effect_without_restart(tmp_path):
    path = tmp_path / "keys.json"
    now = [1_800_000_000.0]
    key, secret = keys.add(path, "robin@vm", "reader", 1, issuer=ISSUER, now=now[0])
    v = verifier(path, clock=lambda: now[0])
    assert asyncio.run(v.verify_token(secret)) is not None
    now[0] += keys.DAY
    assert asyncio.run(v.verify_token(secret)) is None  # expired
    keys.renew(path, key.id, 30, now=now[0])
    assert asyncio.run(v.verify_token(secret)) is not None
    keys.remove(path, key.id)
    assert asyncio.run(v.verify_token(secret)) is None


def test_invalid_or_missing_file_accepts_nothing(tmp_path, caplog):
    path = tmp_path / "keys.json"
    _, secret = keys.add(path, "robin@vm", "writer", 1, issuer=ISSUER)
    v = verifier(path)
    assert asyncio.run(v.verify_token(secret)) is not None
    path.write_text('{"issuer": "key:x", "keys": [{"id": 1}]}')
    assert asyncio.run(v.verify_token(secret)) is None
    path.unlink()
    assert asyncio.run(v.verify_token(secret)) is None
    assert "no key is accepted" in caplog.text and secret not in caplog.text


def test_cli_prints_the_key_once_and_lists_without_it(tmp_path, capsys):
    path = tmp_path / "keys.json"
    keys.main(["add", "--file", str(path), "--issuer", ISSUER, "--subject", "robin@vm",
               "--role", "reader", "--days", "7", "--label", "test"])
    out = capsys.readouterr().out
    secret = next(line for line in out.splitlines() if line.startswith(keys.PREFIX))
    keys.main(["list", "--file", str(path)])
    listing = capsys.readouterr().out
    assert "robin@vm" in listing and "valid" in listing and secret not in listing
    access = asyncio.run(verifier(path).verify_token(secret))
    assert access is not None and access.subject == "robin@vm"
    assert access.claims["boring_caller"]["role"] == "reader"


def test_cli_remove_and_renew_change_the_file(tmp_path, capsys):
    path = tmp_path / "keys.json"
    kept, _ = keys.add(path, "keep@vm", "reader", 1, issuer=ISSUER, now=1_800_000_000)
    gone, secret = keys.add(path, "gone@vm", "writer", 1, now=1_800_000_000)
    before = time.time()
    keys.main(["renew", "--file", str(path), "--id", kept.id, "--days", "10"])
    assert f"renewed {kept.id}" in capsys.readouterr().out
    data = json.loads(path.read_text())
    [renewed] = [k for k in keys.KeyFile.load(path).keys if k.id == kept.id]
    assert int(before) + 10 * keys.DAY <= renewed.expires <= int(time.time()) + 10 * keys.DAY
    assert renewed.sha256 == kept.sha256 and renewed.subject == "keep@vm"
    keys.main(["remove", "--file", str(path), "--id", gone.id])
    assert f"removed {gone.id}" in capsys.readouterr().out
    data = json.loads(path.read_text())
    assert data["issuer"] == ISSUER
    assert [k.id for k in keys.KeyFile.load(path).keys] == [kept.id]
    assert keys.fingerprint(secret) not in path.read_text()
    assert asyncio.run(verifier(path).verify_token(secret)) is None
    with pytest.raises(SystemExit):
        keys.main(["remove", "--file", str(path), "--id", gone.id])
    assert [k.id for k in keys.KeyFile.load(path).keys] == [kept.id]


def config(tmp_path, data):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return ServiceConfig(path)


def test_config_rules(tmp_path):
    with pytest.raises(ValueError, match="issuer, oauth or keys"):
        config(tmp_path, {"url": URL})
    with pytest.raises(ValueError, match="loopback bind"):
        config(tmp_path, {"url": URL, "bind": "0.0.0.0", "keys": {"file": "keys.json"}})
    with pytest.raises(ValueError, match="with keys only"):
        config(tmp_path, {"url": URL, "keys": {"file": "keys.json"}, "access": []})
    https = config(tmp_path, {"url": "https://knowledge.example/mcp", "bind": "0.0.0.0", "port": 18512,
                              "keys": {"file": "keys.json"}})
    assert https.keys is not None and https.verifier is None
    assert https.keys.path == (tmp_path / "keys.json").resolve()


def test_http_keys_only_reader_writer_and_attribution(tmp_path):
    keyfile = tmp_path / "keys.json"
    _, writer = keys.add(keyfile, "robin@vm", "writer", 1, issuer=ISSUER)
    _, reader = keys.add(keyfile, "guest@vm", "reader", 1)
    removed, gone = keys.add(keyfile, "old@vm", "writer", 1)
    keys.remove(keyfile, removed.id)
    effects = []

    async def scenario():
        service = Service()  # its fake control plane stays unused: keys only
        cfg = config(tmp_path, {"url": URL, "keys": {"file": "keys.json"}})
        server = MCPServer("keys-test", **cfg.auth)

        def read():
            return {"author": calling_author()}

        def write():
            effects.append(calling_author())
            return {"author": calling_author()}
        for fn, role, hint in [(read, "reader", READ_ONLY), (write, "writer", WRITES)]:
            server.tool(annotations=hint)(as_mcp_tool(permitted(fn, role), CallLog(None)))
        service.app = wrap_app(server, cfg.url, discover_bypass=False)
        async with service:
            refused, data = await service.result("write", {}, writer)
            assert not refused and json.loads(data["author"]) == [ISSUER, "robin@vm"]
            refused, data = await service.result("write", {}, reader)
            assert refused and data["code"] == "forbidden"
            refused, data = await service.result("read", {}, reader)
            assert not refused and json.loads(data["author"]) == [ISSUER, "guest@vm"]
            for token in (None, gone, "pond", writer + "x"):
                assert (await service.call("read", {}, token)).status_code == 401
            assert service.cp.requests == []  # no introspection for keys
        assert len(effects) == 1
    asyncio.run(scenario())


def test_http_keys_beside_oauth(tmp_path):
    keyfile = tmp_path / "keys.json"
    _, secret = keys.add(keyfile, "robin@vm", "reader", 1, issuer=ISSUER)

    async def scenario():
        service = Service()
        (tmp_path / "secret").write_text("test-only")
        cfg = config(tmp_path, {
            "url": URL, "keys": {"file": "keys.json"},
            "oauth": {"issuer": service.cp.url, "introspection_url": service.cp.url + "/introspect",
                      "client_id": "test-client", "secret_file": "secret"},
            "access": [{"subject": "pond", "role": "writer", "author": "pond"}]})
        service.cp.tokens["pond"] = {"active": True, "aud": cfg.url, "exp": int(time.time()) + 60, "sub": "pond"}
        server = MCPServer("both-test", **cfg.auth)

        def read():
            return {"author": calling_author()}
        server.tool(annotations=READ_ONLY)(as_mcp_tool(permitted(read, "reader"), CallLog(None)))
        service.app = wrap_app(server, cfg.url, discover_bypass=False)
        async with service:
            assert (await service.result("read", {}, "pond")) == (False, {"author": "pond"})
            refused, data = await service.result("read", {}, secret)
            assert not refused and json.loads(data["author"]) == [ISSUER, "robin@vm"]
    asyncio.run(scenario())
