"""Restricted Unix accounts: fake NSS and children, no host identity effects."""
import grp
from types import SimpleNamespace

import pytest

import boring_deploy as deploy


@pytest.fixture
def host(monkeypatch, tmp_path):
    state = {"user": None, "group": None, "extra_groups": [], "others": [], "commands": []}
    def user(name):
        if state["user"] is None:
            raise KeyError(name)
        return state["user"]
    def group(name):
        if state["group"] is None:
            raise KeyError(name)
        return state["group"]
    monkeypatch.setattr(deploy.pwd, "getpwnam", user)
    monkeypatch.setattr(deploy.pwd, "getpwall", lambda: state["others"])
    monkeypatch.setattr(grp, "getgrnam", group)
    monkeypatch.setattr(deploy.os, "getgrouplist", lambda name, gid: [gid, *state["extra_groups"]])
    def run(*args):
        state["commands"].append(args)
        if args[0] == "useradd":
            state["user"] = SimpleNamespace(pw_name="tunnel-box", pw_uid=951, pw_gid=952,
                pw_dir=str(tmp_path / "home"), pw_shell="/usr/sbin/nologin", pw_gecos="Boring tunnel box")
            state["group"] = SimpleNamespace(gr_name="tunnel-box", gr_gid=952, gr_mem=[])
    state.update(run=run, home=tmp_path / "home")
    return state


def create(host):
    return deploy.create_system_account("tunnel-box", host["home"], "Boring tunnel box", host["run"])


def test_create_checks_nss_before_and_after_without_creating_or_deleting_files(host):
    identity = create(host)
    assert identity == deploy.AccountIdentity(951, 952)
    assert host["commands"] == [("useradd", "--system", "--user-group", "--no-create-home",
        "--home-dir", str(host["home"]), "--shell", "/usr/sbin/nologin",
        "--comment", "Boring tunnel box", "tunnel-box")]
    assert not host["home"].exists()


@pytest.mark.parametrize("occupied", ["user", "group", "home", "link"])
def test_creation_refuses_unowned_existing_names_or_paths(host, occupied):
    if occupied in {"user", "group"}:
        host[occupied] = SimpleNamespace()
    elif occupied == "home":
        host["home"].mkdir()
    else:
        host["home"].symlink_to(host["home"].parent / "missing")
    with pytest.raises(deploy.Failure, match="already exists"):
        create(host)
    assert host["commands"] == []


@pytest.mark.parametrize("change", ["uid", "gid", "home", "shell", "comment", "groups", "shared-group"])
def test_changed_identity_cannot_be_locked(host, change):
    identity = create(host)
    host["commands"].clear()
    user = host["user"]
    if change in {"uid", "gid"}:
        setattr(user, "pw_" + change, 953)
    elif change == "home":
        user.pw_dir += "-other"
    elif change == "shell":
        user.pw_shell = "/bin/bash"
    elif change == "comment":
        user.pw_gecos = "another owner"
    elif change == "groups":
        host["extra_groups"] = [999]
    else:
        host["others"] = [SimpleNamespace(pw_name="other", pw_gid=952)]
    with pytest.raises(deploy.Failure, match="identity|restricted"):
        deploy.lock_system_account("tunnel-box", host["home"], "Boring tunnel box", identity, host["run"])
    assert host["commands"] == []


def test_lock_expires_account_as_well_as_password_and_keeps_every_file(host):
    identity = create(host)
    host["home"].mkdir()
    retained = host["home"] / "operator-data"
    retained.write_text("keep")
    host["commands"].clear()
    deploy.lock_system_account("tunnel-box", host["home"], "Boring tunnel box", identity, host["run"])
    assert host["commands"] == [("usermod", "--lock", "--expiredate", "1970-01-02", "tunnel-box")]
    assert retained.read_text() == "keep"


def test_lock_requires_the_recorded_identity(host):
    create(host)
    host["commands"].clear()
    with pytest.raises(deploy.Failure, match="retained identity"):
        deploy.lock_system_account("tunnel-box", host["home"], "Boring tunnel box", None, host["run"])
    assert host["commands"] == []


def test_failed_creation_is_reported_without_guessing_a_success(host):
    with pytest.raises(deploy.Failure, match="not observed"):
        deploy.create_system_account("tunnel-box", host["home"], "Boring tunnel box", lambda *args: None)
