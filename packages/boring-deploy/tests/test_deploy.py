"""Scenarios as an operator meets them: first deploy, upgrade, same version,
refusals, rollback, data generations; venv and docker runtimes against fake
systemctl/docker in a synthetic trusted tool directory (they log calls)."""
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import threading
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

import boring_deploy
from boring_deploy import artifact

FAKE = """#!/bin/sh
echo "$(basename "$0") $*" >> "$FAKE_LOG"
case "$(basename "$0") $1" in
  "systemctl is-active") cat "$FAKE_STATE.$2" 2>/dev/null || cat "$FAKE_STATE" 2>/dev/null || echo active ;;
  "systemctl restart") shift; for unit in "$@"; do rm -f "$FAKE_STATE.$unit"; done ;;
  "docker compose") case "$*" in
      *" ps "*) cat "$FAKE_STATE.compose" 2>/dev/null || echo '{"Service":"svc","State":"running","Health":""}' ;;
      *" config --services"*) cat "$FAKE_STATE.services" 2>/dev/null || echo svc ;;
      *" config --format json"*) sed "s|IMAGE|$BORING_IMAGE|g" "$FAKE_STATE.config" 2>/dev/null \\
          || echo "{\\"services\\": {\\"svc\\": {\\"image\\": \\"$BORING_IMAGE\\"}}}" ;;
      *" up "*) rm -f "$FAKE_STATE.compose" ;;
    esac ;;
  "docker image") [ "$2" = ls ] && [ -n "$FAKE_TAGS" ] && printf '%s\\n' $FAKE_TAGS ;;
esac
exit 0
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    for name in ("systemctl", "docker"):
        (bin_dir / name).write_text(FAKE)
        (bin_dir / name).chmod(0o755)
    monkeypatch.setattr(boring_deploy, "SYSTEM_TOOL_PATH", f"{bin_dir}:/usr/sbin:/usr/bin:/sbin:/bin")
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.log"))
    monkeypatch.setenv("FAKE_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("BORING_DEPLOY_TEST", "1")
    monkeypatch.setattr(boring_deploy, "HOMES", tmp_path / "homes.json")
    monkeypatch.setattr(boring_deploy, "LOCKS", tmp_path / "locks")
    monkeypatch.setattr(boring_deploy, "WAIT_SECONDS", 1)

    def fake_install(ctx, target):  # a real install needs a standalone Python; the trial does that
        (target / "app" / "bin").mkdir(parents=True)
        (target / "app" / "bin" / "python3").write_text("#!/bin/sh\n")
        (target / "app" / boring_deploy.COMPLETE).write_text("")
        (target / "bin").symlink_to("app/bin")
    monkeypatch.setattr(boring_deploy, "install_app", fake_install)
    home = tmp_path / "home"
    home.mkdir()
    return tmp_path, home


class Steps:
    def __init__(self, generation=None, units=None):
        self.generation, self.calls, self._units = generation, [], units

    def units(self, ctx):
        """venv: the unit; docker: the whole stack unless a test names services."""
        if self._units is not None:
            return list(self._units)
        return ["thing.service"] if ctx.release.runtime == "venv" else []

    def check(self, ctx):
        self.calls.append("check")

    def smoke(self, ctx):
        self.calls.append("smoke")

    def data_generation(self, ctx):
        return self.generation

    def backup(self, ctx, directory):
        self.calls.append("backup")
        return directory


def build(tmp, version, runtime="venv", **extra):
    out = tmp / f"build-{version}-{len(list(tmp.glob('build-*')))}"
    out.mkdir()
    steps = out / "steps.py"
    steps.write_text("")
    payload = out / "payload.txt"
    payload.write_text(f"payload {version}\n")
    release = {"component": "thing", "version": version, "runtime": runtime,
               "default_home": "/nonexistent", "python": "python.tar.gz", **extra}
    if runtime == "docker":
        release.update(image="boring-thing")
        files = {"image/Dockerfile": payload}
    else:
        files = {"wheels/requirements.lock": payload, "python.tar.gz": payload}
    files.update({"LICENSE": payload, "COPYING.md": payload})
    archive = artifact.assemble(out, release, steps, files)
    with tarfile.open(archive) as tar:
        tar.extractall(out / "unpacked", filter="data")
    return out / "unpacked" / f"thing-{version}"


def snapshot(home):
    """Everything under HOME: links by target, files by content."""
    found = {}
    for directory, dirs, files in os.walk(home):
        for name in dirs + files:
            path = Path(directory) / name
            relative = path.relative_to(home).as_posix()
            if path.is_symlink():
                found[relative] = ("link", os.readlink(path))
            elif path.is_dir():
                found[relative] = ("dir",)
            else:
                found[relative] = ("file", path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
    return found


def deploy(directory, steps, *argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = boring_deploy.main(directory, steps, list(argv))
    return code, out.getvalue() + err.getvalue()


def test_component_completion_distinguishes_runtime_only_install(env):
    tmp, home = env
    steps = Steps(units=[])
    steps.completion = lambda ctx: "runtime installed; site not configured yet"
    code, out = deploy(build(tmp, "1.0.0"), steps, str(home))
    assert code == 0, out
    assert "7 record  thing 1.0.0 runtime installed; site not configured yet" in out
    assert "is live" not in out
    assert steps.calls == ["check", "smoke"]


def test_first_deploy_upgrade_and_rollback(env):
    tmp, home = env
    steps = Steps()
    code, out = deploy(build(tmp, "1.0.0"), steps, str(home))
    assert code == 0, out
    assert (home / "current").resolve() == home / "releases" / "1.0.0"
    assert (home / "current" / "bin" / "python3").exists()
    assert steps.calls == ["check", "smoke"]
    assert (home / "COMPONENT").read_text() == "thing\n"
    code, out = deploy(build(tmp, "1.1.0"), steps, str(home))
    assert code == 0, out
    assert "back to 1.0.0: sudo" in out and "/previous/deploy" in out
    assert (home / "previous").resolve() == home / "releases" / "1.0.0"
    # rollback: the previous release's own deploy
    code, out = deploy(home / "previous", steps, str(home))
    assert code == 0, out
    assert (home / "current").resolve() == home / "releases" / "1.0.0"
    assert (home / "previous").resolve() == home / "releases" / "1.1.0"
    log = (home / "DEPLOYED").read_text().splitlines()
    assert [line.split()[1:4] for line in log] == [["deploy", "thing", "1.0.0"], ["deploy", "thing", "1.1.0"],
                                                    ["rollback", "thing", "1.0.0"]]
    # a third release removes the one that is neither current nor previous
    assert deploy(build(tmp, "1.2.0"), steps, str(home))[0] == 0
    assert sorted(p.name for p in (home / "releases").iterdir()) == ["1.0.0", "1.2.0"]
    calls = (tmp / "calls.log").read_text()
    assert "systemctl restart thing.service" in calls


def calls(tmp):
    log = tmp / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


@pytest.mark.parametrize("runtime", ["venv", "docker"])
def test_check_is_read_only(env, runtime):
    """--check verifies the release and the host and writes nothing: HOME, homes.json
    and the lock folder are byte for byte as before, no image is built or tagged,
    nothing restarts; the component's check (step 3) is the deploy's."""
    tmp, home = env
    (home / "compose.yaml").write_text("services:\n  svc:\n    image: ${BORING_IMAGE}\n")
    steps = Steps()
    for installed in (False, True):  # a fresh HOME, then one with 1.0.0 running
        if installed:
            assert deploy(build(tmp, "1.0.0", runtime=runtime), Steps(), str(home))[0] == 0
        release = build(tmp, "1.1.0", runtime=runtime)
        before, seen, locks = snapshot(home), len(calls(tmp)), snapshot(tmp / "locks")
        homes = boring_deploy.HOMES.exists()
        code, out = deploy(release, steps, str(home), "--check")
        assert code == 0 and "nothing written" in out and "2 prepare" not in out, out
        assert snapshot(home) == before and snapshot(tmp / "locks") == locks
        assert boring_deploy.HOMES.exists() == homes
        assert [c for c in calls(tmp)[seen:] if c.split()[:2] != ["systemctl", "is-active"]] == []
        assert steps.calls == []
    assert not any("1.1.0" in c for c in calls(tmp))  # never built, tagged or started


@pytest.mark.parametrize("argv", [(), ("--check",)])
def test_preflight_reports_all_problems_before_install_or_prepare(env, monkeypatch, argv):
    from types import SimpleNamespace
    tmp, home = env
    events = []
    steps = Steps()
    steps.preflight = lambda ctx: events.append("preflight") or ["first problem", "second problem"]
    installer = SimpleNamespace(add_options=lambda *args: None,
                                step_zero=lambda *args: events.append("install") or True)
    monkeypatch.setattr(boring_deploy, "_installer", lambda: installer)
    before = snapshot(home)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = boring_deploy.main(build(tmp, "1.0.0"), steps, [str(home), *argv], install=object())
    assert code == 1
    assert "first problem" in out.getvalue() and "second problem" in out.getvalue()
    assert "2 preflight problems" in err.getvalue()
    assert "CHANGELOG Upgrade notes" in err.getvalue()
    assert events == ["preflight"] and steps.calls == []
    assert snapshot(home) == before and calls(tmp) == []


@pytest.mark.parametrize("with_hook", [False, True])
def test_optional_preflight_preserves_existing_install_and_generation_order(env, monkeypatch, with_hook):
    from types import SimpleNamespace
    tmp, home = env
    events = []
    steps = Steps()
    steps.data_generation = lambda ctx: events.append("generation")
    if with_hook:
        steps.preflight = lambda ctx: events.append("preflight") or []
    installer = SimpleNamespace(add_options=lambda *args: None,
                                step_zero=lambda *args: events.append("install") or True)
    monkeypatch.setattr(boring_deploy, "_installer", lambda: installer)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = boring_deploy.main(build(tmp, "1.0.0"), steps, [str(home), "--check"], install=object())
    assert code == 0, err.getvalue()
    assert events == (["preflight"] if with_hook else []) + ["install", "generation"]
    assert snapshot(home) == {} and steps.calls == []


@pytest.mark.parametrize("value", [None, "problem", [1]])
def test_invalid_preflight_reply_is_reported_before_changes(env, value):
    tmp, home = env
    steps = Steps()
    steps.preflight = lambda ctx: value
    code, out = deploy(build(tmp, "1.0.0"), steps, str(home), "--check")
    assert code == 1 and "preflight must return a list" in out
    assert snapshot(home) == {} and steps.calls == []


@pytest.mark.parametrize("runtime", ["venv", "docker"])
def test_artifact_records_glibc_floor_only_for_venv(env, runtime):
    tmp, _ = env
    directory = build(tmp, "1.0.0", runtime=runtime, glibc_min="9.99")
    metadata = json.loads((directory / "RELEASE.json").read_text())
    assert metadata["architecture"] == "x86_64"
    assert boring_deploy.Release.load(directory).architecture == "x86_64"
    if runtime == "venv":
        assert metadata["glibc_min"] == "2.39"
        assert boring_deploy.Release.load(directory).glibc_min == "2.39"
    else:
        assert "glibc_min" not in metadata
        assert boring_deploy.Release.load(directory).glibc_min is None


@pytest.mark.parametrize("argv", [(), ("--check",)])
def test_docker_release_does_not_probe_host_glibc(env, monkeypatch, argv):
    tmp, home = env
    def no_probe(name):
        pytest.fail("the Docker runtime does not use the host glibc")
    monkeypatch.setattr(os, "confstr", no_probe)
    directory = build(tmp, "1.0.0", runtime="docker")
    (home / "compose.yaml").write_text("services:\n  svc:\n    image: ${BORING_IMAGE}\n")
    code, out = deploy(directory, Steps(), str(home), *argv)
    assert code == 0, out


@pytest.mark.parametrize("argv", [(), ("--check",)])
def test_old_glibc_refuses_check_and_deploy_before_prepare(env, monkeypatch, argv):
    tmp, home = env
    monkeypatch.setattr(os, "confstr", lambda name: "glibc 2.38")
    directory = build(tmp, "1.0.0")
    before = snapshot(home)
    steps = Steps()
    code, out = deploy(directory, steps, str(home), *argv)
    assert code == 1 and "glibc 2.39" in out and "glibc 2.38" in out, out
    assert "2 prepare" not in out and steps.calls == []
    assert snapshot(home) == before and calls(tmp) == []


@pytest.mark.parametrize("version", ["2.39", "2.40", "2.100"])
def test_equal_or_newer_glibc_is_accepted(env, monkeypatch, version):
    tmp, home = env
    monkeypatch.setattr(os, "confstr", lambda name: "glibc " + version)
    code, out = deploy(build(tmp, "1.0.0"), Steps(), str(home), "--check")
    assert code == 0 and "nothing written" in out, out


@pytest.mark.parametrize("argv", [(), ("--check",)])
def test_legacy_release_without_glibc_field_skips_host_probe(env, monkeypatch, argv):
    import hashlib
    tmp, home = env
    directory = build(tmp, "1.0.0")
    # Fixture representing an old artifact, including its matching manifest.
    path = directory / "RELEASE.json"
    metadata = json.loads(path.read_text())
    del metadata["glibc_min"]
    path.write_text(json.dumps(metadata))
    manifest = directory / "MANIFEST.sha256"
    manifest.write_text("\n".join(
        hashlib.sha256(path.read_bytes()).hexdigest() + "  RELEASE.json"
        if line.endswith("  RELEASE.json") else line
        for line in manifest.read_text().splitlines()) + "\n")
    def no_probe(name):
        pytest.fail("a legacy release must not probe glibc")
    monkeypatch.setattr(os, "confstr", no_probe)
    assert boring_deploy.Release.load(directory).glibc_min is None
    code, out = deploy(directory, Steps(), str(home), *argv)
    assert code == 0, out


@pytest.mark.parametrize("answer", [None, "", "musl 1.2"])
def test_unknown_glibc_refuses_a_new_artifact(env, monkeypatch, answer):
    tmp, home = env
    monkeypatch.setattr(os, "confstr", lambda name: answer)
    code, out = deploy(build(tmp, "1.0.0"), Steps(), str(home), "--check")
    assert code == 1 and "glibc 2.39" in out and "unknown" in out, out


@pytest.mark.parametrize("floor", [None, True, 2.39, "2", "2.39.1", "bad"])
def test_release_rejects_malformed_glibc_requirement(tmp_path, floor):
    metadata = {"component": "thing", "version": "1.0.0", "runtime": "venv",
                "default_home": "/unused", "glibc_min": floor}
    (tmp_path / "RELEASE.json").write_text(json.dumps(metadata))
    with pytest.raises(boring_deploy.Failure, match="glibc_min"):
        boring_deploy.Release.load(tmp_path)


@pytest.mark.parametrize("argv", [(), ("--check",)])
def test_wrong_architecture_refuses_before_first_install_planning(env, monkeypatch, argv):
    from types import SimpleNamespace
    tmp, home = env
    monkeypatch.setattr(boring_deploy.platform, "machine", lambda: "aarch64")
    def unexpected(*args):
        pytest.fail("first-install planning must not run for a wrong-architecture artifact")
    monkeypatch.setattr(boring_deploy, "_installer", lambda: SimpleNamespace(
        add_options=lambda *args: None, step_zero=unexpected))
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = boring_deploy.main(build(tmp, "1.0.0"), Steps(), [str(home), *argv], install=object())
    assert code == 1 and "artifact architecture is x86_64" in err.getvalue()
    assert "host architecture is aarch64" in err.getvalue()
    assert snapshot(home) == {} and calls(tmp) == []


def test_glibc_refusal_precedes_first_install(env, monkeypatch):
    from types import SimpleNamespace
    tmp, home = env
    monkeypatch.setattr(os, "confstr", lambda name: "glibc 2.38")
    def unexpected(*args):
        pytest.fail("first install must not run on an unsupported host")
    monkeypatch.setattr(boring_deploy, "_installer", lambda: SimpleNamespace(
        add_options=lambda *args: None, step_zero=unexpected))
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = boring_deploy.main(build(tmp, "1.0.0"), Steps(), [str(home)], install=object())
    assert code == 1 and "glibc 2.38" in err.getvalue()
    assert snapshot(home) == {}


def test_check_refuses_what_the_deploy_would_and_names_a_data_change(env):
    tmp, home = env
    (home / "compose.yaml").write_text("services:\n  svc:\n    image: boring-thing:0.0.1\n")
    before = snapshot(home)
    code, out = deploy(build(tmp, "1.0.0", runtime="docker"), Steps(), str(home), "--check")
    assert code == 1 and "${BORING_IMAGE}" in out and snapshot(home) == before
    code, out = deploy(build(tmp, "2.0.0", data_generation=5, upgrades_from=4), Steps(4), str(home), "--check")
    assert code == 0 and "the data changes from generation 4 to 5" in out and snapshot(home) == before
    code, out = deploy(build(tmp, "2.0.0", data_generation=5, upgrades_from=5), Steps(4), str(home), "--check")
    assert code == 1 and "intermediate release" in out and snapshot(home) == before


def test_tampered_release_and_foreign_home_are_refused(env):
    tmp, home = env
    release = build(tmp, "1.0.0")
    (release / "wheels" / "requirements.lock").write_text("changed\n")
    before = snapshot(home)
    code, out = deploy(release, Steps(), str(home))
    assert code == 1 and "does not match MANIFEST" in out and "nothing changed" in out
    assert snapshot(home) == before
    (home / "COMPONENT").write_text("other\n")
    before = snapshot(home)
    code, out = deploy(build(tmp, "1.0.0"), Steps(), str(home))
    assert code == 1 and "belongs to other" in out
    assert snapshot(home) == before


@pytest.mark.parametrize("runtime", ["venv", "docker"])
def test_a_copy_that_differs_from_the_manifest_is_refused_and_removed(env, monkeypatch, runtime):
    """The source passed verify; the copy in HOME is what runs, so it is checked too."""
    tmp, home = env
    (home / "compose.yaml").write_text("services:\n  svc:\n    image: ${BORING_IMAGE}\n")
    release = build(tmp, "1.0.0", runtime=runtime)
    copytree = boring_deploy.shutil.copytree

    def copy_then_change(source, target, *args, **kw):  # the source changing while it is copied
        copytree(source, target, *args, **kw)
        if args:  # shutil's own recursion into a subdirectory
            return
        for name in ("wheels/requirements.lock", "image/Dockerfile"):
            if (Path(target) / name).exists():
                (Path(target) / name).write_text("changed\n")
    monkeypatch.setattr(boring_deploy.shutil, "copytree", copy_then_change)
    before = snapshot(home)
    code, out = deploy(release, Steps(), str(home))
    assert code == 1 and "does not match MANIFEST" in out
    if runtime == "venv":
        assert sorted(p.name for p in (home / "releases").iterdir()) == []
        assert snapshot(home) == {**before, "releases": ("dir",)}
    else:
        assert snapshot(home) == before


def test_same_version_with_different_files_is_refused(env):
    tmp, home = env
    assert deploy(build(tmp, "1.0.0"), Steps(), str(home))[0] == 0
    manifest = (home / "releases" / "1.0.0" / "MANIFEST.sha256").read_bytes()
    before = snapshot(home)
    other = build(tmp, "1.0.0", note="rebuilt")
    code, out = deploy(other, Steps(), str(home))
    assert code == 1 and "different files" in out
    assert (home / "releases" / "1.0.0" / "MANIFEST.sha256").read_bytes() == manifest
    assert os.readlink(home / "current") == before["current"][1]
    assert snapshot(home) == before


def test_docker_same_version_keeps_distinct_previous_and_rollback_image(env, monkeypatch):
    tmp, home = env
    (home / "compose.yaml").write_text("services:\n  svc:\n    image: ${BORING_IMAGE}\n")
    monkeypatch.setenv("FAKE_TAGS", "1.0.0 1.1.0")
    first, second = build(tmp, "1.0.0", runtime="docker"), build(tmp, "1.1.0", runtime="docker")
    for release in (first, second, second):
        code, out = deploy(release, Steps(), str(home))
        assert code == 0, out
    assert (home / "release" / "VERSION").read_text().strip() == "1.1.0"
    assert (home / "release.previous" / "VERSION").read_text().strip() == "1.0.0"
    assert "back to 1.0.0" in out
    assert "docker image rm boring-thing:1.0.0" not in calls(tmp)
    code, out = deploy(home / "release.previous", Steps(), str(home))
    assert code == 0, out
    assert (home / "release" / "VERSION").read_text().strip() == "1.0.0"
    assert (home / "release.previous" / "VERSION").read_text().strip() == "1.1.0"
    assert (home / boring_deploy.IMAGE_ENV).read_text() == "BORING_IMAGE=boring-thing:1.0.0\n"


@pytest.mark.parametrize("version", ["1.0.0", "1.1.0"])
def test_docker_refuses_rebuilt_current_or_previous_version_before_build(env, version):
    tmp, home = env
    (home / "compose.yaml").write_text("services:\n  svc:\n    image: ${BORING_IMAGE}\n")
    for deployed in ("1.0.0", "1.1.0"):
        code, out = deploy(build(tmp, deployed, runtime="docker"), Steps(), str(home))
        assert code == 0, out
    other = build(tmp, version, runtime="docker", note="rebuilt")
    before, commands = snapshot(home), calls(tmp)
    code, out = deploy(other, Steps(), str(home))
    assert code == 1 and "different files" in out and "tag a new one" in out
    assert snapshot(home) == before and calls(tmp) == commands


@pytest.mark.parametrize("runtime", ["venv", "docker"])
def test_failed_check_retains_prepared_release_without_claiming_no_writes(env, runtime):
    tmp, home = env
    (home / "compose.yaml").write_text("services:\n  svc:\n    image: ${BORING_IMAGE}\n")
    assert deploy(build(tmp, "1.0.0", runtime=runtime), Steps(), str(home))[0] == 0
    class BadCheck(Steps):
        def check(self, ctx):
            raise boring_deploy.Failure("candidate check failed")
    code, out = deploy(build(tmp, "1.1.0", runtime=runtime), BadCheck(), str(home))
    assert code == 1 and "candidate check failed" in out
    assert "nothing that runs changed; the prepared release stays for the next attempt" in out
    assert "nothing changed:" not in out
    prepared = home / ("release.new" if runtime == "docker" else "releases/1.1.0")
    current = home / ("release" if runtime == "docker" else "current")
    assert (prepared / "VERSION").read_text().strip() == "1.1.0"
    assert (current / "VERSION").read_text().strip() == "1.0.0"


def test_requires(env):
    tmp, home = env
    other = tmp / "croc"
    (other / "current").mkdir(parents=True)
    (other / "current" / "VERSION").write_text("0.4.4\n")
    boring_deploy.HOMES.write_text(json.dumps({"boring-crocodile": str(other)}))
    before = snapshot(home)
    code, out = deploy(build(tmp, "1.0.0", requires={"boring-crocodile": ">=0.4.5"}), Steps(), str(home))
    assert code == 1 and "needs boring-crocodile >=0.4.5" in out and "has 0.4.4" in out
    assert snapshot(home) == before
    (other / "current" / "VERSION").write_text("0.4.5\n")
    assert deploy(build(tmp, "1.0.0", requires={"boring-crocodile": ">=0.4.5"}), Steps(), str(home))[0] == 0


@pytest.mark.parametrize("installed", ["0.4.21", "0.6.0"])
def test_minor_line_requires_refuses_before_prepare_or_switch(env, installed):
    tmp, home = env
    other = tmp / "croc"
    (other / "current").mkdir(parents=True)
    (other / "current" / "VERSION").write_text(installed + "\n")
    boring_deploy.HOMES.write_text(json.dumps({"boring-crocodile": str(other)}))
    before = snapshot(home)
    steps = Steps()
    release = build(tmp, "1.0.0", requires={"boring-crocodile": ">=0.5.0,<0.6"})
    code, out = deploy(release, steps, str(home))
    assert code == 1 and "needs boring-crocodile >=0.5.0,<0.6" in out
    assert snapshot(home) == before
    assert steps.calls == []


def test_data_generations(env, monkeypatch):
    tmp, home = env
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # no terminal
    before = snapshot(home)
    code, out = deploy(build(tmp, "2.0.0", data_generation=5, upgrades_from=4), Steps(4), str(home))
    assert code == 1 and "pass --backup or --no-backup" in out
    assert snapshot(home) == before
    steps = Steps(4)
    assert deploy(build(tmp, "2.0.0", data_generation=5, upgrades_from=4), steps, str(home), "--backup")[0] == 0
    assert steps.calls == ["check", "backup", "smoke"] and (home / "backups").is_dir()
    before = snapshot(home)
    manifest = (home / "current" / "MANIFEST.sha256").read_bytes()
    code, out = deploy(build(tmp, "1.0.0", data_generation=4), Steps(5), str(home))
    assert code == 1 and "restoring the backup" in out
    assert snapshot(home) == before
    code, out = deploy(build(tmp, "3.0.0", data_generation=7, upgrades_from=6), Steps(5), str(home))
    assert code == 1 and "intermediate release" in out
    assert snapshot(home) == before
    assert (home / "current").resolve() == home / "releases" / "2.0.0"
    assert (home / "current" / "MANIFEST.sha256").read_bytes() == manifest


def test_a_failed_release_never_becomes_previous(env):
    tmp, home = env
    assert deploy(build(tmp, "1.0.0"), Steps(), str(home))[0] == 0
    (tmp / "state").write_text("failed\n")
    assert deploy(build(tmp, "1.1.0"), Steps(), str(home))[0] == 1  # switched, then failed
    (tmp / "state").unlink()
    code, out = deploy(build(tmp, "1.2.0"), Steps(), str(home))
    assert code == 0
    assert (home / "previous").resolve() == home / "releases" / "1.0.0"
    assert sorted(p.name for p in (home / "releases").iterdir()) == ["1.0.0", "1.2.0"]
    # The way back names the release previous really holds, not the failed one.
    assert "back to 1.0.0: sudo" in out and "1.1.0" not in out.split("is live", 1)[1]


def test_a_release_healthy_before_the_switch_becomes_previous_whatever_the_record_says(env):
    tmp, home = env
    assert deploy(build(tmp, "1.0.0"), Steps(), str(home))[0] == 0
    (tmp / "state").write_text("failed\n")
    assert deploy(build(tmp, "1.1.0"), Steps(), str(home))[0] == 1  # not recorded as good
    (tmp / "state").unlink()  # the operator fixed what made it fail: 1.1.0 runs fine now

    class Healthy(Steps):
        def healthy(self, ctx):
            return True
    assert deploy(build(tmp, "1.2.0"), Healthy(), str(home))[0] == 0
    assert (home / "previous").resolve() == home / "releases" / "1.1.0"


def test_docker_failed_release_is_dropped_and_old_images_pruned(env, monkeypatch):
    tmp, home = env
    (home / "compose.yaml").write_text("services:\n  svc:\n    image: ${BORING_IMAGE}\n")
    assert deploy(build(tmp, "1.0.0", runtime="docker"), Steps(), str(home))[0] == 0
    failing = Steps()
    failing.smoke = lambda ctx: (_ for _ in ()).throw(boring_deploy.Failure("smoke"))
    assert deploy(build(tmp, "1.1.0", runtime="docker"), failing, str(home))[0] == 1
    seen = len((tmp / "calls.log").read_text().splitlines())
    monkeypatch.setenv("FAKE_TAGS", "1.0.0 1.1.0 1.2.0 0.9.0 <none>")  # what `docker image ls` lists
    assert deploy(build(tmp, "1.2.0", runtime="docker"), Steps(), str(home))[0] == 0
    assert (home / "release.previous" / "VERSION").read_text() == "1.0.0\n"
    assert "docker image ls boring-thing" in (tmp / "calls.log").read_text()
    removed = [line for line in (tmp / "calls.log").read_text().splitlines()[seen:]
               if line.startswith("docker image rm")]
    assert removed == ["docker image rm boring-thing:1.1.0", "docker image rm boring-thing:0.9.0"]


def test_a_unit_that_fails_names_the_rollback(env):
    tmp, home = env
    assert deploy(build(tmp, "1.0.0"), Steps(), str(home))[0] == 0
    (tmp / "state").write_text("failed\n")
    code, out = deploy(build(tmp, "1.1.0"), Steps(), str(home))
    assert code == 1 and "FAILED at start" in out and "back to 1.0.0" in out
    assert (home / "current").resolve() == home / "releases" / "1.1.0"
    assert (home / "previous").resolve() == home / "releases" / "1.0.0"
    assert (home / "previous" / "bin" / "python3").exists()


@pytest.mark.parametrize("runtime", ["venv", "docker"])
@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("own", [None, "follow the component's recovery procedure"])
def test_component_way_back_replaces_the_hint_on_success_and_failure(env, runtime, failed, own):
    tmp, home = env
    if runtime == "docker":
        (home / "compose.yaml").write_text("services:\n  svc:\n    image: ${BORING_IMAGE}\n")
    assert deploy(build(tmp, "1.0.0", runtime=runtime), Steps(), str(home))[0] == 0
    seen = []
    steps = Steps()
    steps.way_back = lambda ctx, previous: seen.append(previous.resolve()) or own
    if failed:
        steps.smoke = lambda ctx: (_ for _ in ()).throw(boring_deploy.Failure("synthetic smoke failure"))
    code, out = deploy(build(tmp, "1.1.0", runtime=runtime), steps, str(home))
    assert code == int(failed)
    previous = home / boring_deploy.RUNTIMES[runtime].PREVIOUS
    assert seen == [previous.resolve()]
    normal = f"back to 1.0.0: sudo {previous}/deploy {home}"
    assert (own or normal) in out
    if own is not None:
        assert normal not in out


def test_first_install_has_no_way_back_hook(env):
    tmp, home = env
    steps = Steps()
    steps.way_back = lambda ctx, previous: pytest.fail("no retained release")
    assert deploy(build(tmp, "1.0.0"), steps, str(home))[0] == 0


def test_a_failed_way_back_hook_preserves_the_deploy_failure_without_a_false_command(env):
    tmp, home = env
    assert deploy(build(tmp, "1.0.0"), Steps(), str(home))[0] == 0
    steps = Steps()
    steps.smoke = lambda ctx: (_ for _ in ()).throw(boring_deploy.Failure("synthetic smoke failure"))
    steps.way_back = lambda ctx, previous: (_ for _ in ()).throw(boring_deploy.Failure("recovery unreadable"))
    code, out = deploy(build(tmp, "1.1.0"), steps, str(home))
    assert code == 1 and "FAILED at smoke: synthetic smoke failure" in out
    assert "way back unavailable: recovery unreadable" in out
    assert "sudo" not in out


def test_docker_runtime(env):
    tmp, home = env
    (home / "compose.yaml").write_text("services:\n  svc:\n    image: boring-thing:0.0.1\n")
    code, out = deploy(build(tmp, "1.0.0", runtime="docker"), Steps(), str(home))
    assert code == 1 and "${BORING_IMAGE}" in out
    (home / "compose.yaml").write_text("services:\n  svc:\n    image: ${BORING_IMAGE}\n")
    (home / ".env").write_text("SOURCECACHE_UID=1\n")
    assert deploy(build(tmp, "1.0.0", runtime="docker"), Steps(), str(home))[0] == 0
    assert deploy(build(tmp, "1.1.0", runtime="docker"), Steps(), str(home))[0] == 0
    assert (home / ".env").read_text() == "SOURCECACHE_UID=1\n"
    assert (home / "boring-image.env").read_text() == "BORING_IMAGE=boring-thing:1.1.0\n"
    assert (home / "release" / "VERSION").read_text() == "1.1.0\n"
    assert (home / "release.previous" / "VERSION").read_text() == "1.0.0\n"
    code, out = deploy(home / "release.previous", Steps(), str(home))
    assert code == 0, out
    assert (home / "boring-image.env").read_text().endswith("BORING_IMAGE=boring-thing:1.0.0\n")
    assert (home / ".env").read_text() == "SOURCECACHE_UID=1\n"
    assert (home / "release" / "VERSION").read_text() == "1.0.0\n"
    calls = (tmp / "calls.log").read_text()
    assert "docker build --quiet -t boring-thing:1.1.0" in calls and "compose -f" in calls


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o400])
def test_docker_migrates_only_its_image_line_and_keeps_private_operator_env(env, mode):
    tmp, home = env
    (home / "compose.yaml").write_text("services:\n  svc:\n    image: ${BORING_IMAGE}\n")
    original = b"# operator\r\nTOKEN=synthetic-secret\r\nBORING_IMAGE=old:1\r\nOTHER=x"
    operator = home / ".env"
    operator.write_bytes(original)
    operator.chmod(mode)
    owner = (operator.stat().st_uid, operator.stat().st_gid)
    release = build(tmp, "1.0.0", runtime="docker")
    assert deploy(release, Steps(), str(home), "--check")[0] == 0
    assert operator.read_bytes() == original and stat.S_IMODE(operator.stat().st_mode) == mode
    code, out = deploy(release, Steps(), str(home))
    assert code == 0, out
    assert operator.read_bytes() == b"# operator\r\nTOKEN=synthetic-secret\r\nOTHER=x"
    assert stat.S_IMODE(operator.stat().st_mode) == mode
    assert (operator.stat().st_uid, operator.stat().st_gid) == owner
    assert (home / boring_deploy.IMAGE_ENV).read_text() == "BORING_IMAGE=boring-thing:1.0.0\n"
    command = boring_deploy.compose_command(home, "up", "-d")
    assert command[-6:] == ["--env-file", str(operator), "--env-file", str(home / boring_deploy.IMAGE_ENV), "up", "-d"]


def test_image_migration_keeps_an_empty_env_file_and_does_not_touch_unowned_lines(tmp_path):
    path = tmp_path / ".env"
    path.write_bytes(b"BORING_IMAGE=old:1\n")
    path.chmod(0o600)
    boring_deploy.migrate_image_env(path)
    assert path.exists() and path.read_bytes() == b""
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.write_bytes(b"export BORING_IMAGE=operator\n# BORING_IMAGE=comment\nSECRET=x")
    before = path.stat()
    boring_deploy.migrate_image_env(path)
    assert path.stat().st_ino == before.st_ino and path.stat().st_mtime_ns == before.st_mtime_ns


def test_docker_build_context_has_fresh_mtimes(env):
    """Every release's files carry the artifact's fixed mtime and the context path
    is always HOME/release.new; BuildKit took a same-size file with the same mtime
    there as unchanged (a newer release built with the older requirements.txt).
    So the context the build sees is copied with mtimes of now."""
    tmp, home = env
    (home / "compose.yaml").write_text("services:\n  svc:\n    image: ${BORING_IMAGE}\n")
    release = build(tmp, "1.0.0", runtime="docker")
    assert (release / "image" / "Dockerfile").stat().st_mtime == artifact.EPOCH
    before = os.stat(tmp).st_mtime - 1  # the file system's clock, not time.time()
    assert deploy(release, Steps(), str(home))[0] == 0
    assert (home / "release" / "image" / "Dockerfile").stat().st_mtime >= before


def test_an_update_leaves_a_stopped_site_stopped(env):
    """`systemctl disable --now` holds across updates: an inactive unit is not
    restarted, and when none runs, start and smoke are skipped and said so."""
    tmp, home = env
    steps = Steps()
    assert deploy(build(tmp, "1.0.0"), steps, str(home))[0] == 0
    (tmp / "state.thing.service").write_text("inactive\n")
    seen = len(calls(tmp))
    code, out = deploy(build(tmp, "1.1.0"), steps, str(home))
    assert code == 0, out
    assert "site is stopped: not started" in out and "smoke  skipped" in out
    assert "1.1.0 is installed, not started" in out
    assert not [c for c in calls(tmp)[seen:] if c.startswith("systemctl restart")]
    assert steps.calls == ["check", "smoke", "check"]  # no smoke for the stopped site
    assert (home / "current").resolve() == home / "releases" / "1.1.0"
    assert (home / "DEPLOYED").read_text().splitlines()[-1].split()[1:4] == ["deploy", "thing", "1.1.0"]


EXITED = '{"Service":"svc","State":"exited","Health":""}\n'


@pytest.mark.parametrize("stack", [EXITED, "", "[" + EXITED.strip() + "]"])  # stopped, down, older Compose
def test_an_update_leaves_a_stopped_docker_stack_stopped(env, stack):
    """`compose stop` (or down) holds across updates: the new image goes into boring-image.env,
    nothing is brought up, start and smoke are skipped and said so."""
    tmp, home = env
    (home / "compose.yaml").write_text("services:\n  svc:\n    image: ${BORING_IMAGE}\n")
    steps = Steps()
    assert deploy(build(tmp, "1.0.0", runtime="docker"), steps, str(home))[0] == 0
    (tmp / "state.compose").write_text(stack)
    seen = len(calls(tmp))
    code, out = deploy(build(tmp, "1.1.0", runtime="docker"), steps, str(home))
    assert code == 0, out
    assert "site is stopped: not started" in out and "smoke  skipped" in out
    assert f"1.1.0 is installed, not started (start it with docker compose -f {home}/compose.yaml --env-file {home}/boring-image.env up -d)" in out
    assert not [c for c in calls(tmp)[seen:] if " up " in f"{c} "]
    assert steps.calls == ["check", "smoke", "check"]
    assert not (home / ".env").exists()
    assert (home / "boring-image.env").read_text() == "BORING_IMAGE=boring-thing:1.1.0\n"
    assert (home / "release" / "VERSION").read_text() == "1.1.0\n"
    assert (home / "DEPLOYED").read_text().splitlines()[-1].split()[1:4] == ["deploy", "thing", "1.1.0"]


def test_a_running_docker_stack_is_updated_and_a_first_install_starts(env):
    tmp, home = env
    (home / "compose.yaml").write_text("services:\n  svc:\n    image: ${BORING_IMAGE}\n")
    (tmp / "state.compose").write_text("")  # nothing runs before the first install
    steps = Steps()
    code, out = deploy(build(tmp, "1.0.0", runtime="docker"), steps, str(home))
    assert code == 0 and "1.0.0 is live" in out, out
    assert any(c.endswith(" up -d") for c in calls(tmp)) and not (tmp / "state.compose").exists()
    seen = len(calls(tmp))
    code, out = deploy(build(tmp, "1.1.0", runtime="docker"), steps, str(home))
    assert code == 0 and "1.1.0 is live" in out, out
    assert [c for c in calls(tmp)[seen:] if c.endswith(" up -d")] and steps.calls == ["check", "smoke"] * 2


def test_docker_units_name_the_services_the_deploy_owns(env):
    """A stack with a front end and backends it does not build: units name the
    front end; whether it runs, `up -d` and the wait ask about it alone."""
    tmp, home = env
    (home / "compose.yaml").write_text(
        "services:\n  svc:\n    image: ${BORING_IMAGE}\n  backend:\n    image: other:1\n")
    (tmp / "state.compose").write_text("")
    steps = Steps(units=["svc"])
    code, out = deploy(build(tmp, "1.0.0", runtime="docker"), steps, str(home))
    assert code == 0 and "1.0.0 is live" in out, out
    log = calls(tmp)
    assert any(c.endswith(" up -d svc") for c in log), log
    assert not any(c.endswith(" up -d") for c in log)
    assert any(c.endswith("ps --all --format json svc") for c in log)
    assert not any(c.endswith("ps --all --format json") for c in log)


def test_docker_units_refuse_unknown_names_before_the_switch(env):
    tmp, home = env
    (home / "compose.yaml").write_text("services:\n  svc:\n    image: ${BORING_IMAGE}\n")
    (tmp / "state.services").write_text("svc\nbackend\n")
    code, out = deploy(build(tmp, "1.0.0", runtime="docker"), Steps(units=["front"]), str(home))
    assert code == 1 and "units names no service" in out and "front" in out and "svc, backend" in out, out
    assert not (home / ".env").exists() and not [c for c in calls(tmp) if " up " in f"{c} "]


def test_docker_units_refuse_an_unnamed_service_with_the_image(env):
    tmp, home = env
    (home / "compose.yaml").write_text("services:\n  svc:\n    image: ${BORING_IMAGE}\n")
    (tmp / "state.services").write_text("svc\nworker\nbackend\n")
    (tmp / "state.config").write_text(
        '{"services": {"svc": {"image": "IMAGE"}, "worker": {"image": "IMAGE"}, "backend": {"image": "other:1"}}}')
    code, out = deploy(build(tmp, "1.0.0", runtime="docker"), Steps(units=["svc"]), str(home))
    assert code == 1 and "worker also use ${BORING_IMAGE}" in out and "backend" not in out.split("FAILED")[1], out
    assert not (home / ".env").exists() and not [c for c in calls(tmp) if " up " in f"{c} "]
    assert "nothing that runs changed; the prepared release stays for the next attempt" in out


def test_docker_units_stopped_front_end_stays_stopped(env):
    """The named service exited (its backend may run): an update leaves it
    stopped and says which services."""
    tmp, home = env
    (home / "compose.yaml").write_text("services:\n  svc:\n    image: ${BORING_IMAGE}\n")
    steps = Steps(units=["svc"])
    assert deploy(build(tmp, "1.0.0", runtime="docker"), steps, str(home))[0] == 0
    (tmp / "state.compose").write_text(EXITED)
    seen = len(calls(tmp))
    code, out = deploy(build(tmp, "1.1.0", runtime="docker"), steps, str(home))
    assert code == 0, out
    assert "site is stopped: not started" in out and "(svc)" in out
    assert not [c for c in calls(tmp)[seen:] if " up " in f"{c} "]


def test_an_update_restarts_only_what_runs_and_what_an_install_made(env):
    tmp, home = env

    class Two(Steps):
        def units(self, ctx):
            return ["a.service", "b.service", "new.service"]

        def check(self, ctx):
            super().check(ctx)
            ctx.start.append("new.service")  # as an install step does for a unit it made
    (tmp / "state.b.service").write_text("inactive\n")
    (tmp / "state.new.service").write_text("inactive\n")
    (tmp / "state.a.service").write_text("active\n")
    code, out = deploy(build(tmp, "1.0.0"), Two(), str(home))
    assert code == 0, out
    assert "systemctl restart a.service new.service" in calls(tmp)
    assert "not started: b.service" in out and "systemctl is-active b.service" in calls(tmp)
    # waited for what was restarted only: b stays inactive and the deploy still succeeds
    assert "1.0.0 is live" in out


def test_a_change_before_the_switch_is_reported_not_nothing_changed(env):
    """A units hook that wrote host files and then failed: the engine names what
    the step recorded in ctx.changed instead of "nothing changed"."""
    tmp, home = env

    class Partial(Steps):
        def units(self, ctx):
            ctx.changed.append("wrote a.service (backup in /tmp/b)")
            raise boring_deploy.Failure("the second unit could not be written")
    code, out = deploy(build(tmp, "1.0.0"), Partial(), str(home))
    assert code == 1 and "the second unit could not be written" in out, out
    assert "nothing changed" not in out
    assert "host changed before the switch" in out and "wrote a.service (backup in /tmp/b)" in out


def held_deploy(tmp, home, release, steps_class=Steps):
    """A deploy in a thread that stops inside its check (lock held) until released."""
    inside, go, result = threading.Event(), threading.Event(), {}

    class Holding(steps_class):
        def check(self, ctx):
            super().check(ctx)
            inside.set()
            assert go.wait(30)
    thread = threading.Thread(target=lambda: result.update(outcome=deploy(release, Holding(), str(home))))
    thread.start()
    assert inside.wait(30)
    return thread, go, result


def test_a_second_deploy_of_one_home_refuses_and_the_first_completes(env):
    """The reviewer's interleaving: A prepares 1.0.0, B would prepare 1.1.0 and A's
    switch would prune it. With the operation lock B refuses before it reads
    anything; A completes and keeps exactly what it should."""
    tmp, home = env
    first, second = build(tmp, "1.0.0"), build(tmp, "1.1.0")
    thread, go, result = held_deploy(tmp, home, first)
    try:
        code, out = deploy(second, Steps(), str(home))
        assert code == 1 and f"another deploy or uninstall of {home} is in progress" in out
        assert "nothing changed" in out and sorted(p.name for p in (home / "releases").iterdir()) == ["1.0.0"]
        code, out = deploy(second, Steps(), str(home), "--check")  # a check refuses too, writing nothing
        assert code == 1 and "in progress" in out
    finally:
        go.set()
        thread.join(30)
    code, out = result["outcome"]
    assert code == 0, out
    assert (home / "current").resolve() == home / "releases" / "1.0.0"
    assert (home / "current" / "bin" / "python3").exists()
    assert sorted(p.name for p in (home / "releases").iterdir()) == ["1.0.0"]
    assert [line.split()[3] for line in (home / "DEPLOYED").read_text().splitlines()] == ["1.0.0"]
    assert deploy(second, Steps(), str(home))[0] == 0  # once it is done, the next one runs
    assert (home / "previous").resolve() == home / "releases" / "1.0.0"


def test_one_lock_per_folder_whatever_names_it(env):
    tmp, home = env
    (tmp / "alias").symlink_to(home)
    assert boring_deploy.lock_path(tmp / "alias") == boring_deploy.lock_path(home)
    assert boring_deploy.lock_path(home) != boring_deploy.lock_path(tmp / "other")


def test_an_unsafe_lock_folder_is_refused(env):
    tmp, home = env
    (tmp / "locks").mkdir(mode=0o777)
    os.chmod(tmp / "locks", 0o777)
    code, out = deploy(build(tmp, "1.0.0"), Steps(), str(home))
    assert code == 1 and "unsafe lock folder" in out and not (home / "releases").exists()


def test_the_launcher_refuses_python_older_than_3_12(env, monkeypatch):
    """One clear line, before the engine or the steps are imported."""
    tmp, _ = env
    release = build(tmp, "1.0.0")
    monkeypatch.setattr(sys, "version_info", (3, 11, 17, "final", 0))

    def no_exec(*args):
        raise AssertionError("went on past the version check")
    monkeypatch.setattr(os, "execv", no_exec)
    for name in ("deploy",):
        source = (release / name).read_text()
        with pytest.raises(SystemExit) as stop:
            exec(compile(source, str(release / name), "exec"), {"__name__": "__main__", "__file__": str(release / name)})
        assert stop.value.code == "python3 3.12 or newer is needed to run this; this is python3 3.11"


OLD_PYTHONS = sorted(Path.home().glob(".local/share/uv/python/cpython-3.11*/bin/python3.11"))


@pytest.mark.skipif(not OLD_PYTHONS, reason="no Python 3.11 here (uv python install 3.11)")
def test_the_launcher_under_a_real_python_3_11(env):
    tmp, _ = env
    release = build(tmp, "1.0.0")
    result = subprocess.run([str(OLD_PYTHONS[0]), str(release / "deploy"), "--help"], capture_output=True, text=True)
    assert result.returncode == 1 and result.stdout == ""
    assert result.stderr == "python3 3.12 or newer is needed to run this; this is python3 3.11\n"


def test_artifact_is_reproducible_and_launcher_runs(env):
    tmp, _ = env
    first, second = build(tmp, "1.0.0"), build(tmp, "1.0.0")
    a = first.parent.parent / "thing-1.0.0-linux-x86_64.tar.gz"
    b = second.parent.parent / "thing-1.0.0-linux-x86_64.tar.gz"
    assert a.read_bytes() == b.read_bytes()
    result = subprocess.run([sys.executable, str(first / "deploy"), "--help"], capture_output=True, text=True)
    assert result.returncode == 0 and "Deploy thing 1.0.0" in result.stdout
    assert os.stat(first / "deploy").st_mode & stat.S_IXUSR


def test_python_tarball_modes_ignore_the_builders_umask(tmp_path, monkeypatch):
    fake = tmp_path / "py" / "bin"
    fake.mkdir(parents=True)
    (fake / "python3.14").write_text("#!/bin/sh\n")
    (fake / "python3.14").chmod(0o750)
    (tmp_path / "py" / "lib.py").write_text("x")
    (tmp_path / "py" / "lib.py").chmod(0o640)
    (tmp_path / "py" / "EXTERNALLY-MANAGED").write_text("")
    (tmp_path / "py" / "BUILD").write_text("20990101")
    notices = tmp_path / "notices" / "3.14.8+20990101"
    notices.mkdir(parents=True)
    (notices / "LICENSE.openssl-3.txt").write_text("Apache\n")
    (notices / "LICENSE.openssl-3.txt").chmod(0o600)
    monkeypatch.setattr(artifact, "PYTHON_NOTICES", tmp_path / "notices")
    uv = tmp_path / "uv"
    uv.write_text(f"#!/bin/sh\n[ \"$2\" = find ] && echo {fake}/python3.14\nexit 0\n")
    uv.chmod(0o755)
    archive = artifact.standalone_python(str(uv), "3.14.8", tmp_path / "python.tar.gz")
    with tarfile.open(archive) as tar:
        modes = {m.name: m.mode for m in tar.getmembers()}
    assert modes["python/lib.py"] == 0o644 and modes["python/bin/python3.14"] == 0o755
    assert modes["python/licenses"] == 0o755 and modes["python/licenses/LICENSE.openssl-3.txt"] == 0o644
    assert modes["python"] == 0o755 and "python/EXTERNALLY-MANAGED" not in modes


def test_a_release_without_its_licence_is_refused(tmp_path):
    (tmp_path / "steps.py").write_text("")
    with pytest.raises(SystemExit, match="lacks COPYING.md"):
        artifact.assemble(tmp_path, {"component": "thing", "version": "1.0.0"}, tmp_path / "steps.py",
                          {"LICENSE": tmp_path / "steps.py"})
    assert not (tmp_path / "thing-1.0.0").exists()


def test_python_tarball_needs_the_licence_notices(tmp_path, monkeypatch):
    top = tmp_path / "py"
    top.mkdir()
    (top / "BUILD").write_text("20990101\n")
    monkeypatch.setattr(artifact, "PYTHON_NOTICES", tmp_path / "notices")
    with pytest.raises(SystemExit, match=r"3\.14\.8\+20990101.*python_notices\.py 3\.14\.8 20990101"):
        artifact.python_notices(top, "3.14.8")
    kept = tmp_path / "notices" / "3.14.8+20990101"
    kept.mkdir(parents=True)
    (kept / "LICENSE.zlib.txt").write_text("zlib\n")
    assert artifact.python_notices(top, "3.14.8") == kept


def test_install_app_rejects_archive_paths_outside_the_unpack_directory(tmp_path):
    target = tmp_path / "release"
    target.mkdir()
    with tarfile.open(target / "python.tar.gz", "w:gz") as tar:
        tar.addfile(tarfile.TarInfo("../escaped"))
    release = boring_deploy.Release(target, "thing", "1.0.0", "venv", "/nonexistent",
                                    details={"python": "python.tar.gz"})
    ctx = boring_deploy.Context(release, tmp_path, None, lambda message: None)
    with pytest.raises(tarfile.OutsideDestinationError):
        boring_deploy.install_app(ctx, target)
    assert not (target / "escaped").exists()
    assert not (tmp_path / "escaped").exists()


def test_install_app_unpacks_python_with_service_modes(tmp_path, monkeypatch):
    source = tmp_path / "src" / "python"
    (source / "bin").mkdir(parents=True)
    (source / "lib").mkdir()
    (source / "bin" / "python3").write_text("#!/bin/sh\n")
    (source / "bin" / "python3").chmod(0o700)
    (source / "bin" / "python").symlink_to("python3")
    (source / "lib" / "x.py").write_text("x = 1\n")
    (source / "lib" / "x.py").chmod(0o600)
    (source / "lib").chmod(0o700)
    target = tmp_path / "releases" / "1.0.0"
    (target / "wheels").mkdir(parents=True)
    with tarfile.open(target / "python.tar.gz", "w:gz") as tar:
        tar.add(source, arcname="python")
    release = boring_deploy.Release(target, "thing", "1.0.0", "venv", "/nonexistent",
                                    details={"python": "python.tar.gz", "app_wheels": ["thing.whl"]})
    ctx = boring_deploy.Context(release, tmp_path, None, lambda message: None)
    calls = []
    ctx.run = lambda *command, **kw: calls.append(command)
    symlink_to = Path.symlink_to
    def interrupt_link(path, destination, **options):
        if path == target / "bin":
            assert not (target / "app" / boring_deploy.COMPLETE).exists()
            raise OSError("interrupted before bin publication")
        return symlink_to(path, destination, **options)
    with monkeypatch.context() as interrupted:
        interrupted.setattr(Path, "symlink_to", interrupt_link)
        with pytest.raises(OSError, match="before bin publication"):
            boring_deploy.install_app(ctx, target)
    assert not (target / "app" / boring_deploy.COMPLETE).exists()
    calls.clear()
    boring_deploy.install_app(ctx, target)
    app = target / "app"
    python = str(app / "bin" / "python3")
    assert (app / boring_deploy.COMPLETE).read_text() == "1.0.0\n"
    assert os.readlink(target / "bin") == "app/bin"
    assert (target / "bin" / "python3").read_text() == "#!/bin/sh\n"
    mode = lambda p: stat.S_IMODE(p.lstat().st_mode)
    assert mode(app) == 0o755 and mode(app / "bin") == 0o755 and mode(app / "lib") == 0o755
    assert mode(app / "bin" / "python3") == 0o755
    assert mode(app / "lib" / "x.py") == 0o644
    assert os.readlink(app / "bin" / "python") == "python3"
    assert sorted(p.name for p in target.iterdir()) == ["app", "bin", "python.tar.gz", "wheels"]  # no temp dir
    assert calls[0][:2] == (python, "-I") and "--require-hashes" in calls[0]
    assert calls[0][-2:] == ("-r", str(target / "wheels" / "requirements.lock"))
    assert calls[1][-1] == str(target / "wheels" / "thing.whl")
    assert calls[2] == (python, "-I", "-m", "pip", "check")
    assert calls[3][:3] == (python, "-I", "-c") and "compileall" in calls[3][3]
    assert len(calls) == 4


@pytest.mark.parametrize("prior", ["absent", "wrong-link", "file"])
def test_prepare_repairs_an_old_completion_marker_without_bin(tmp_path, prior):
    target = tmp_path / "releases/1.0.0"
    (target / "app/bin").mkdir(parents=True)
    (target / "app/bin/python3").write_text("completed runtime fixture")
    (target / "app" / boring_deploy.COMPLETE).write_text("1.0.0\n")
    if prior == "wrong-link":
        (target / "bin").symlink_to("elsewhere")
    elif prior == "file":
        (target / "bin").write_text("foreign entry")
    release = boring_deploy.Release(target, "thing", "1.0.0", "venv", "/nonexistent")
    ctx = boring_deploy.Context(release, tmp_path, None, lambda message: None)
    if prior != "absent":
        with pytest.raises(boring_deploy.Failure, match="unexpected bin link"):
            boring_deploy.Venv().prepare(ctx)
        return
    boring_deploy.Venv().prepare(ctx)
    assert (target / "bin/python3").read_text() == "completed runtime fixture"
    assert os.readlink(target / "bin") == "app/bin"


def test_versions():
    key = boring_deploy.version_key
    assert key("0.3.0-dev.7") < key("0.3.0-rc.1") < key("0.3.0") < key("0.3.1")
    assert key("0.3.0.dev7") == key("0.3.0-dev.7") and key("0.2.0rc15") == key("0.2.0-rc.15")
    assert boring_deploy.satisfies("0.4.5", ">=0.4.5,<0.5")
    assert not boring_deploy.satisfies("0.5.0", ">=0.4.5,<0.5")


def test_a_reused_release_in_home_is_verified_and_kept(env):
    tmp, home = env
    release = build(tmp, "1.0.0")
    assert deploy(release, Steps(), str(home))[0] == 0
    listed = (home / "releases" / "1.0.0" / "MANIFEST.sha256").read_text().splitlines()[0].split("  ", 1)[1]
    kept = home / "releases" / "1.0.0" / listed
    kept.write_text("damaged\n")  # the copy in HOME, not the unpacked release
    code, out = deploy(release, Steps(), str(home))
    assert code == 1 and "does not match MANIFEST" in out
    assert kept.read_text() == "damaged\n"  # reported, never removed: it may be the running one
    assert (home / "current").resolve() == home / "releases" / "1.0.0"


def _private(root):
    """Modes a release gets when unpacked under umask 0027, as in a builder's home."""
    for path in [root, *root.rglob("*")]:
        if not path.is_symlink():
            path.chmod(0o750 if path.is_dir() or os.access(path, os.X_OK) else 0o640)


def _modes(root):
    return {oct(p.stat().st_mode & 0o777) for p in [root, *root.rglob("*")] if not p.is_symlink()}


def test_a_release_unpacked_privately_is_installed_readable_by_the_services(env):
    """Seen live: a release folder of 0750 was copied as it was, only
    app/ was opened up, and the controller's account could not enter
    releases/VERSION (203/EXEC). Now the whole copy is 0755/0644, and so is a
    copy an older deploy left with private modes."""
    tmp, home = env
    release = build(tmp, "1.0.0")
    _private(release)
    assert deploy(release, Steps(), str(home))[0] == 0
    target = home / "releases" / "1.0.0"
    assert _modes(target) <= {"0o755", "0o644"}
    assert oct((target / "deploy").stat().st_mode & 0o777) == "0o755"
    _private(target)  # as the failed first attempt left it
    assert deploy(release, Steps(), str(home))[0] == 0
    assert _modes(target) <= {"0o755", "0o644"}


def test_a_step_command_never_reads_the_operators_stdin(tmp_path):
    """The deploy's stdin (here a pipe holding what the operator typed,
    standing in for the terminal) is not a step child's: it reads /dev/null.
    Before, `docker compose run` in the services' check kept the terminal as
    stdin and was stopped by it, and the deploy hung. (The pipe is closed and
    holds data, so the old behaviour fails this test instead of blocking it.)"""
    ctx = boring_deploy.Context(None, tmp_path, None, print)
    probe = (sys.executable, "-I", "-c",
             "import os, sys; print(os.readlink('/proc/self/fd/0')); print(repr(sys.stdin.read()))")
    read_end, write_end = os.pipe()
    os.write(write_end, b"typed")
    os.close(write_end)
    saved = os.dup(0)
    try:
        os.dup2(read_end, 0)
        assert ctx.run(*probe).stdout.splitlines() == ["/dev/null", "''"]
        inherited = ctx.run(*probe, interactive=True).stdout.splitlines()  # only when asked for
        assert inherited[0].startswith("pipe:") and inherited[1] == "'typed'"  # left unread above
    finally:
        os.dup2(saved, 0)
        for fd in (saved, read_end):
            os.close(fd)


def test_home_check_refuses_writable_ancestry_before_install(tmp_path):
    from boring_deploy import Failure, check_home_path

    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(0o775)
    with pytest.raises(Failure, match=r"shared.*permits another writer"):
        check_home_path(parent / "component", missing_ok=True)
    parent.chmod(0o755)
    home = parent / "component"
    home.mkdir()
    # HOME's contents belong to the component. Do not walk service-owned data.
    (home / "service-config").symlink_to("/some/component/path")
    check_home_path(home)
    alias = parent / "alias"
    alias.symlink_to(home, target_is_directory=True)
    with pytest.raises(Failure, match=r"unsafe component HOME .*alias"):
        check_home_path(alias)
