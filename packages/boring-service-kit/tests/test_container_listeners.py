"""Deploy owns container listeners; native runs retain their loopback guard."""
import json
import re
import sqlite3
from unittest.mock import patch

import pytest
from boring_service_kit.http import ServiceConfig


def config(tmp_path, mode="keys", name="config.json"):
    value = {"url":"https://service.example.test/mcp", "mode":mode}
    if mode == "keys":
        value["keys"] = {"file":"keys.json"}
    elif mode == "issuer":
        value["issuer"] = {"subject":"owner", "role":"writer", "database":"private/oauth.sqlite",
                           "approval_secret_file":"approval"}
    else:
        value["crocodile"] = {"url":"https://authority.example.test", "client_id":"fixture",
                             "secret_file":"secret"}
        (tmp_path / "secret").write_text("fixture-secret")
    path = tmp_path / name
    path.write_text(json.dumps(value))
    return path, value


def deploy(monkeypatch, mapping):
    monkeypatch.setenv("BORING_SERVICE_CONTAINER", "1")
    monkeypatch.setenv("BORING_SERVICE_LISTENERS", json.dumps(mapping))


@pytest.mark.parametrize("mode", ["keys", "issuer", "crocodile"])
def test_container_listener_comes_from_deploy_and_inspection_uses_same_owner(tmp_path, monkeypatch, mode):
    path, _ = config(tmp_path, mode)
    deploy(monkeypatch, {str(path):{"bind":"0.0.0.0", "port":18512}})
    with patch.object(sqlite3, "connect", side_effect=AssertionError("opened issuer store")):
        info = ServiceConfig.inspect(path)
    assert (info.bind, info.port) == ("0.0.0.0", 18512)
    assert not (tmp_path / "private").exists()
    if mode != "issuer":
        live = ServiceConfig(path)
        assert live.info() == info
        assert not hasattr(live, "behind_proxy")


@pytest.mark.parametrize("key,value", [("bind","127.0.0.1"), ("port",18512), ("behind_proxy",False)])
def test_container_refuses_superseded_json_fields_with_the_deploy_replacement(tmp_path, monkeypatch, key, value):
    path, data = config(tmp_path)
    path.write_text(json.dumps(data | {key:value}))
    deploy(monkeypatch, {str(path):{"bind":"0.0.0.0", "port":18512}})
    with pytest.raises(ValueError, match=key + ".*Compose.*BORING_SERVICE_LISTENERS"):
        ServiceConfig.validate(path)


@pytest.mark.parametrize("other_listener", [False, True], ids=["empty", "other-listener"])
def test_missing_container_listener_is_named_never_guessed(tmp_path, monkeypatch, other_listener):
    path, _ = config(tmp_path)
    mapping = {str(tmp_path / "other.json"): {"bind": "0.0.0.0", "port": 8000}} if other_listener else {}
    deploy(monkeypatch, mapping)
    with pytest.raises(ValueError, match=r"BORING_SERVICE_LISTENERS.*" + re.escape(str(path))):
        ServiceConfig.validate(path)


@pytest.mark.parametrize("listener", [None, [], {"bind":"0.0.0.0"}, {"bind":"","port":1},
    {"bind":4,"port":1}, {"bind":"0.0.0.0","port":True}, {"bind":"0.0.0.0","port":0},
    {"bind":"0.0.0.0","port":65536}, {"bind":"0.0.0.0","port":8000,"extra":True}])
def test_bad_deploy_listener_is_refused(tmp_path, monkeypatch, listener):
    path, _ = config(tmp_path)
    deploy(monkeypatch, {str(path):listener})
    with pytest.raises(ValueError, match="BORING_SERVICE_LISTENERS"):
        ServiceConfig.validate(path)


def test_frontends_read_their_own_deploy_address(tmp_path, monkeypatch):
    first, _ = config(tmp_path)
    second, _ = config(tmp_path, name="gateway.json")
    deploy(monkeypatch, {str(first):{"bind":"172.16.46.2","port":18520},
                         str(second):{"bind":"172.16.46.2","port":18521}})
    assert ServiceConfig.inspect(first).port == 18520
    assert ServiceConfig.inspect(second).port == 18521


def test_native_listener_cannot_silently_use_container_settings(tmp_path, monkeypatch):
    path, _ = config(tmp_path)
    monkeypatch.delenv("BORING_SERVICE_CONTAINER", raising=False)
    monkeypatch.setenv("BORING_SERVICE_LISTENERS", "{}")
    with pytest.raises(ValueError, match="requires BORING_SERVICE_CONTAINER=1"):
        ServiceConfig.validate(path)
