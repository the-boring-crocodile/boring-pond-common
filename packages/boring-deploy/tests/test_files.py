"""Configuration-only releases use the normal deploy sequence without an app."""
from pathlib import Path
import pytest
import boring_deploy as deploy
from boring_deploy import artifact
from test_deploy import env, deploy as run, snapshot  # noqa: F401


def release(tmp, version):
    root = tmp / ("source-" + version)
    root.mkdir()
    payload = root / "licence"
    payload.write_text("fixture")
    steps = root / "steps.py"
    steps.write_text("")
    artifact.assemble(root, {"component": "thing", "version": version, "runtime": "files",
        "default_home": "/nonexistent"}, steps, {"LICENSE": payload, "COPYING.md": payload})
    return root / ("thing-" + version)


def test_files_check_deploy_update_and_rollback_need_no_python_or_container(env, monkeypatch):
    tmp, home = env
    first, second = release(tmp, "1.0.0"), release(tmp, "1.1.0")
    monkeypatch.setattr(deploy, "install_app", lambda *a: pytest.fail("created a runtime"))
    before = snapshot(home)
    assert run(first, object(), str(home), "--check")[0] == 0
    assert snapshot(home) == before
    assert run(first, object(), str(home))[0] == 0
    assert deploy.installed_version(home, "files") == "1.0.0"
    assert not (home / "current/app").exists()
    assert run(second, object(), str(home))[0] == 0
    assert (home / "previous/VERSION").read_text().strip() == "1.0.0"
    assert run(home / "previous", object(), str(home))[0] == 0
    assert deploy.installed_version(home, "files") == "1.0.0"
