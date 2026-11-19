"""Read-only config validation shares startup's parser, without opening issuer state."""
import json
import sqlite3
from unittest.mock import patch

import pytest

from boring_service_kit.http import ServiceConfig


def configuration(path, mode="issuer"):
    value = {"url": "https://service.example.test/mcp", "mode": mode}
    if mode == "issuer":
        value["issuer"] = {"subject": "owner", "role": "writer", "database": "private/oauth.sqlite3",
                           "approval_secret_file": "approval"}
        (path.parent / "approval").write_text("synthetic-approval-secret-for-tests")
    elif mode == "crocodile":
        value["crocodile"] = {"url": "https://authority.example.test",
                             "client_id": "synthetic-client", "secret_file": "secret"}
        (path.parent / "secret").write_text("synthetic-secret")
    else:
        value["keys"] = {"file": "keys.json"}
    path.write_text(json.dumps(value))
    return value


@pytest.mark.parametrize("mode", ["issuer", "crocodile", "keys"])
def test_validation_reads_configuration_without_opening_state(tmp_path, mode):
    path = tmp_path / "config.json"
    configuration(path, mode)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with patch("boring_service_kit.http.SQLiteIssuer",
               side_effect=AssertionError("opened issuer during validation")), \
         patch.object(sqlite3, "connect", side_effect=AssertionError("opened database")):
        assert ServiceConfig.validate(path) is None
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


def test_validation_does_not_change_existing_issuer_state_but_startup_opens_it(tmp_path):
    path = tmp_path / "config.json"
    configuration(path)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    database = private / "oauth.sqlite3"
    assert ServiceConfig.validate(path) is None
    assert not database.exists()
    live = ServiceConfig(path)
    assert live.issuer is not None and database.exists()
    before = (database.read_bytes(), database.stat().st_mtime_ns)
    with patch.object(sqlite3, "connect", side_effect=AssertionError("read live state")):
        assert ServiceConfig.validate(path) is None
    assert before == (database.read_bytes(), database.stat().st_mtime_ns)


@pytest.mark.parametrize("change", [
    {"unexpected": True}, {"port": True}, {"behind_proxy": "true"},
    {"bind": "0.0.0.0"}, {"access": [{"subject": "owner", "role": "administrator"}]},
    {"issuer": {"subject": "owner", "role": "administrator", "database": "db", "approval_secret_file": "secret"}},
    {"issuer": {"subject": "owner", "role": "writer", "database": "", "approval_secret_file": "secret"}},
    {"issuer": {"subject": "owner", "role": "writer", "database": "db", "approval_secret_file": ""}},
    {"issuer": {"subject": "owner", "role": "writer", "database": "db", "approval_secret_file": "secret",
                "dynamic_registration": "yes"}},
    {"issuer": {"subject": "owner", "role": "writer", "database": "db", "approval_secret_file": "secret",
                "metadata_document_hosts": [5]}},
    {"issuer": {"subject": "owner", "url": "http://public.example.test", "database": "db",
                "approval_secret_file": "secret"}},
    {"issuer": {"subject": "owner", "name": "", "role": "writer", "database": "db", "approval_secret_file": "secret"}},
    {"pond": {"url": "https://authority.example.test"}},
    {"oauth": {"issuer": "https://authority.example.test"}},
])
def test_validation_and_startup_reject_the_same_invalid_configuration(tmp_path, change):
    path = tmp_path / "config.json"
    value = configuration(path)
    path.write_text(json.dumps(value | change))
    with pytest.raises((ValueError, KeyError, OSError)) as startup:
        ServiceConfig(path)
    with pytest.raises(type(startup.value)) as validation:
        ServiceConfig.validate(path)
    assert str(validation.value) == str(startup.value)
    assert not (tmp_path / "private").exists()


@pytest.mark.parametrize("mode", ["issuer", "crocodile", "keys"])
def test_inspection_returns_only_immutable_frontend_metadata(tmp_path, mode):
    from dataclasses import asdict, FrozenInstanceError
    path = tmp_path / "config.json"
    configuration(path, mode)
    with patch.object(sqlite3, "connect", side_effect=AssertionError("opened issuer state")):
        info = ServiceConfig.inspect(path)
    assert asdict(info) == {
        "url": "https://service.example.test/mcp", "bind": "127.0.0.1", "port": 8000,
        "client_id": "synthetic-client" if mode == "crocodile" else None,
        "authority_url": "https://authority.example.test" if mode == "crocodile" else None}
    assert not hasattr(info, "serve") and not hasattr(info, "token_verifier")
    with pytest.raises(FrozenInstanceError):
        info.port = 1


def test_validation_includes_sdk_issuer_url_checks_without_touching_state(tmp_path):
    path = tmp_path / "config.json"
    value = configuration(path)
    value["issuer"]["url"] = "https://authority.example.test:invalid"
    path.write_text(json.dumps(value))
    with patch.object(sqlite3, "connect", side_effect=AssertionError("opened database")):
        with pytest.raises(ValueError):
            ServiceConfig.validate(path)
    assert not (tmp_path / "private").exists()


@pytest.mark.parametrize("mode", ["issuer", "crocodile", "keys"])
def test_planned_config_inspection_needs_no_config_or_runtime_state(tmp_path, mode):
    path = tmp_path / "config.json"
    value = configuration(path, mode)
    expected = ServiceConfig.inspect(path)
    path.unlink()
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with patch.object(sqlite3, "connect", side_effect=AssertionError("opened database")):
        assert ServiceConfig.inspect_value(value, path) == expected
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert not path.exists()


def test_planned_config_and_file_parser_refuse_the_same_bad_authority(tmp_path):
    path = tmp_path / "config.json"
    value = configuration(path, "crocodile")
    value["crocodile"]["url"] = "https://authority.example.test?secret=synthetic"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError) as file_error:
        ServiceConfig.inspect(path)
    path.unlink()
    with pytest.raises(ValueError) as planned_error:
        ServiceConfig.inspect_value(value, path)
    assert str(planned_error.value) == str(file_error.value)
    assert not path.exists()
