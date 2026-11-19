"""Installed dependency checks are shared by required installs and optional peers."""
import json
from pathlib import Path

import pytest

import boring_deploy


@pytest.mark.parametrize("version,accepted", [
    ("0.3.11", False), ("0.4.0rc7", False), ("0.4.0rc8", True),
    ("0.4.0rc9", True), ("0.4.0", True), ("0.5.0", False),
])
@pytest.mark.parametrize("runtime", ["venv", "docker"])
def test_optional_peer_checks_an_installed_version(tmp_path, monkeypatch, version, accepted, runtime):
    home = tmp_path / "peer"
    marker = home / ("current" if runtime == "venv" else "release") / "VERSION"
    marker.parent.mkdir(parents=True)
    marker.write_text(version)
    homes = tmp_path / "homes.json"
    homes.write_text(json.dumps({"boring-pirates": str(home)}))
    monkeypatch.setattr(boring_deploy, "HOMES", homes)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    if accepted:
        boring_deploy.check_dependency("boring-pirates", ">=0.4.0rc8,<0.5", optional=True)
    else:
        with pytest.raises(boring_deploy.Failure, match="needs boring-pirates"):
            boring_deploy.check_dependency("boring-pirates", ">=0.4.0rc8,<0.5", optional=True)
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_optional_peer_does_not_require_an_install(tmp_path, monkeypatch):
    monkeypatch.setattr(boring_deploy, "HOMES", tmp_path / "absent-homes.json")
    # A unique default HOME avoids observing another component installed on this host.
    boring_deploy.check_dependency("absent-synthetic-component", ">=1.0,<2", optional=True)
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(boring_deploy.Failure, match="none found"):
        boring_deploy.check_dependency("absent-synthetic-component", ">=1.0,<2")


def test_recorded_peer_without_a_version_is_not_treated_as_absent(tmp_path, monkeypatch):
    homes = tmp_path / "homes.json"
    homes.write_text(json.dumps({"peer": str(tmp_path / "missing")}))
    monkeypatch.setattr(boring_deploy, "HOMES", homes)
    with pytest.raises(boring_deploy.Failure, match="none found"):
        boring_deploy.check_dependency("peer", ">=1.0,<2", optional=True)
