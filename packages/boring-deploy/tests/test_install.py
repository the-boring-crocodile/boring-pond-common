"""Step 0 and uninstall as an operator meets them: a first install asked for,
refused without a terminal, done with --install; settings only at the first
install; irregular hosts stop; uninstall keeps state and lists it."""
import io
import json
import os
import stat
import subprocess
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

import boring_deploy
from boring_deploy import artifact
from boring_deploy import install as installing
from test_deploy import Steps, build, deploy, env, held_deploy, snapshot  # noqa: F401  (env is a fixture)


class FakeInstall:
    """Installed means: HOME/compose.yaml exists (the file install writes last)."""

    SETTINGS = [installing.Setting("public-url", "an https URL", pattern=r"https://[a-z.]+"),
                installing.Setting("web-bind", "an IP address", default="127.0.0.1"),
                installing.Setting("unsafe-url-auth", "tokens in links", flag=True)]

    def __init__(self, irregular=(), fail=False):
        self.irregular, self.fail, self.calls = list(irregular), fail, []

    def look(self, ctx):
        compose = ctx.home / "compose.yaml"
        notes = ["state/ from an earlier install: reused"] if (ctx.home / "state").exists() else []
        return installing.Look(compose.exists(), list(self.irregular), notes)

    def ready(self, ctx, settings):
        if settings.get("public_url", "").endswith(".invalid"):
            raise boring_deploy.Failure("the edge does not know that host")

    def plan(self, ctx, settings):
        return [f"compose.yaml for {settings['public_url']} on {settings.get('web_bind')}"]

    def install(self, ctx, settings):
        self.calls.append(("install", settings))
        ctx.home.mkdir(parents=True, exist_ok=True)
        (ctx.home / "state").mkdir(exist_ok=True)
        if self.fail:
            raise boring_deploy.Failure("useradd exited 9")
        (ctx.home / "compose.yaml").write_text("services:\n  svc:\n    image: ${BORING_IMAGE}\n")

    def uninstall(self, ctx, delete_data):
        self.calls.append(("uninstall", delete_data))
        (ctx.home / "compose.yaml").unlink()
        return [str(ctx.home / "state")]


def assert_public_file(path):
    """Written whole: mode 0644 and no .new file left beside it."""
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert not path.with_name(path.name + ".new").exists()


def docker(tmp, version="1.0.0"):
    return build(tmp, version, runtime="docker")


def uninstall(directory, steps, *argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = installing.uninstall_main(directory, steps, list(argv))
    return code, out.getvalue() + err.getvalue()


def run(tmp, home, fake, *argv, version="1.0.0", home_name="svc"):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = boring_deploy.main(docker(tmp, version), Steps(), [str(home / home_name), *argv], install=fake)
    return code, out.getvalue() + err.getvalue()


def test_first_install_needs_install_without_a_terminal(env):
    tmp, home = env
    fake = FakeInstall()
    code, out = run(tmp, home, fake, "--public-url", "https://canvas.example")
    assert code == 1 and "pass --install" in out and "nothing changed" in out
    assert fake.calls == [] and not (home / "svc").exists()


def test_first_install_names_missing_settings(env):
    tmp, home = env
    code, out = run(tmp, home, FakeInstall(), "--install")
    assert code == 1 and "--public-url (an https URL)" in out
    code, out = run(tmp, home, FakeInstall(), "--install", "--public-url", "http://plain")
    assert code == 1 and "is not an https URL" in out
    index = installing.Setting("index", "an index file", file=True)
    (tmp / "d365.db").write_text("")
    assert index.check(str(tmp / "d365.db")) == str((tmp / "d365.db").resolve())
    with pytest.raises(boring_deploy.Failure, match="is not a file"):
        index.check(str(home))


def test_ready_refuses_before_anything_changes(env):
    tmp, home = env
    fake = FakeInstall()
    code, out = run(tmp, home, fake, "--install", "--public-url", "https://canvas.invalid")
    assert code == 1 and "the edge does not know that host" in out and "nothing changed" in out
    assert fake.calls == []
    assert not boring_deploy.HOMES.exists() and not (home / "svc").exists()


def test_check_shows_the_plan_and_changes_nothing(env):
    tmp, home = env
    fake = FakeInstall()
    code, out = run(tmp, home, fake, "--check", "--public-url", "https://canvas.example")
    assert code == 0, out
    assert "compose.yaml for https://canvas.example on 127.0.0.1" in out and "nothing written" in out
    assert fake.calls == []
    assert not boring_deploy.HOMES.exists() and not (home / "svc").exists()
    assert not boring_deploy.LOCKS.exists()  # not even a lock file


def test_install_then_deploy_then_settings_are_refused(env):
    tmp, home = env
    fake = FakeInstall()
    code, out = run(tmp, home, fake, "--install", "--public-url", "https://canvas.example", "--unsafe-url-auth")
    assert code == 0, out
    assert fake.calls == [("install", {"public_url": "https://canvas.example", "web_bind": "127.0.0.1",
                                       "unsafe_url_auth": True})]
    assert json.loads(boring_deploy.HOMES.read_text()) == {"thing": str(home / "svc")}
    assert_public_file(boring_deploy.HOMES)
    assert_public_file(home / "svc" / ".env")
    assert (home / "svc" / ".env").read_text() == "BORING_IMAGE=boring-thing:1.0.0\n"
    assert "7 record  thing 1.0.0 is live" in out
    code, out = run(tmp, home, fake, version="1.1.0")
    assert code == 0 and "installed" in out and len(fake.calls) == 1
    code, out = run(tmp, home, fake, "--public-url", "https://other.example", version="1.2.0")
    assert code == 1 and "settings are for a first install only: --public-url" in out
    assert (home / "svc" / "release" / "VERSION").read_text() == "1.1.0\n"


def test_irregular_stops_before_anything(env):
    tmp, home = env
    fake = FakeInstall(irregular=["container boring-svc exists without compose.yaml"])
    code, out = run(tmp, home, fake, "--install", "--public-url", "https://canvas.example")
    assert code == 1 and "irregular" in out and "container boring-svc" in out
    assert fake.calls == [] and "nothing changed" in out
    assert not boring_deploy.HOMES.exists() and not (home / "svc").exists()


def test_settings_may_depend_on_the_release_and_a_foreign_home_is_refused(env):
    tmp, home = env
    fake = FakeInstall()
    settings = fake.SETTINGS
    fake.SETTINGS = lambda release: settings[:1] if release.component == "thing" else []
    (home / "svc").mkdir()
    (home / "svc" / "COMPONENT").write_text("boring-other\n")
    code, out = run(tmp, home, fake, "--install", "--public-url", "https://canvas.example")
    assert code == 1 and "belongs to boring-other" in out and fake.calls == []
    (home / "svc" / "COMPONENT").unlink()
    code, out = run(tmp, home, fake, "--install", "--public-url", "https://canvas.example")
    assert code == 0, out
    assert fake.calls == [("install", {"public_url": "https://canvas.example"})]


def test_a_home_recorded_elsewhere_is_not_installed_twice(env):
    tmp, home = env
    (home / "real").mkdir()
    boring_deploy.HOMES.write_text(json.dumps({"thing": str(home / "real")}))
    recorded = boring_deploy.HOMES.read_bytes()
    code, out = run(tmp, home, FakeInstall(), "--install", "--public-url", "https://canvas.example")
    assert code == 1 and "recorded at" in out and str(home / "real") in out
    assert boring_deploy.HOMES.read_bytes() == recorded and not (home / "svc").exists()


def test_a_home_recorded_by_another_install_meanwhile_is_kept(env):
    """Two first installs of one component into two HOMEs hold two operation
    locks: both pass step zero's check. The one that records second refuses
    under homes.lock instead of re-pointing the other's line."""
    tmp, home = env

    class Meanwhile(FakeInstall):
        def install(self, ctx, settings):
            super().install(ctx, settings)
            (home / "real").mkdir()  # the other install finished and recorded itself
            with boring_deploy.homes_update() as known:
                known["thing"] = str(home / "real")
    code, out = run(tmp, home, Meanwhile(), "--install", "--public-url", "https://canvas.example")
    assert code == 1 and "while this install ran" in out and "uninstall" in out
    assert json.loads(boring_deploy.HOMES.read_text()) == {"thing": str(home / "real")}
    assert not (home / "svc" / "release").exists()  # stopped before its first deploy
    assert uninstall(docker(tmp), FakeInstall(), str(home / "svc"), "--yes")[0] == 0
    assert json.loads(boring_deploy.HOMES.read_text()) == {"thing": str(home / "real")}  # not its line


def test_an_install_that_stops_part_way_says_so_and_a_rerun_reuses_it(env):
    tmp, home = env
    code, out = run(tmp, home, FakeInstall(fail=True), "--install", "--public-url", "https://canvas.example")
    assert code == 1 and "run the deploy again" in out and "nothing changed" not in out
    assert "uninstall" not in out  # it would find nothing installed: not a way out
    assert not boring_deploy.HOMES.exists()  # a home is recorded only once the install finished
    assert not (home / "svc" / "compose.yaml").exists()
    fake = FakeInstall()
    code, out = run(tmp, home, fake, "--install", "--public-url", "https://canvas.example")
    assert code == 0 and "state/ from an earlier install: reused" in out
    assert json.loads(boring_deploy.HOMES.read_text()) == {"thing": str(home / "svc")}
    assert_public_file(boring_deploy.HOMES)
    assert_public_file(home / "svc" / ".env")
    assert (home / "svc" / "release" / "VERSION").read_text() == "1.0.0\n"


def test_uninstall_keeps_state_and_lists_it(env):
    tmp, home = env
    fake = FakeInstall()
    release = docker(tmp)
    code, out = uninstall(release, fake, str(home / "svc"), "--yes")
    assert code == 0 and "not installed here" in out
    assert run(tmp, home, fake, "--install", "--public-url", "https://canvas.example")[0] == 0
    code, out = uninstall(release, fake, str(home / "svc"))
    assert code == 1 and "pass --yes" in out and (home / "svc" / "compose.yaml").exists()
    code, out = uninstall(release, fake, str(home / "svc"), "--yes", "--delete-data")
    assert code == 0, out
    assert fake.calls[-1] == ("uninstall", True)
    assert "no longer live" in out and str(home / "svc" / "state") in out
    assert (home / "svc" / "DEPLOYED").read_text().splitlines()[-1].split()[1] == "uninstall"
    assert "removed thing from" in out and json.loads(boring_deploy.HOMES.read_text()) == {}


def test_after_uninstall_another_home_may_be_chosen(env):
    tmp, home = env
    fake = FakeInstall()
    url = ("--install", "--public-url", "https://canvas.example")
    assert run(tmp, home, fake, *url)[0] == 0
    boring_deploy.HOMES.write_text(json.dumps({"thing": str(home / "svc"), "other": "/opt/other"}))
    assert uninstall(docker(tmp), fake, str(home / "svc"), "--yes")[0] == 0
    assert json.loads(boring_deploy.HOMES.read_text()) == {"other": "/opt/other"}  # only its own line
    assert (home / "svc" / "state").exists()  # the HOME stays
    code, out = run(tmp, home, fake, *url, version="1.1.0", home_name="moved")
    assert code == 0, out
    assert json.loads(boring_deploy.HOMES.read_text())["thing"] == str(home / "moved")


@pytest.mark.skipif(sys.version_info < (3, 11), reason="the deploy launcher needs Python 3.11 or newer")
def test_release_with_install_steps_carries_uninstall(env, tmp_path):
    steps = tmp_path / "install_steps.py"
    steps.write_text("from boring_install import Setting\nSETTINGS = [Setting('config-dir', 'a directory',"
                     " directory=True)]\n")
    out = tmp_path / "out"
    out.mkdir()
    (tmp_path / "steps.py").write_text("")
    archive = artifact.assemble(out, {"component": "thing", "version": "1.0.0", "runtime": "docker",
                                                    "default_home": "/nonexistent", "image": "boring-thing"},
                                              tmp_path / "steps.py", {"LICENSE": tmp_path / "steps.py",
                                                                      "COPYING.md": tmp_path / "steps.py"},
                                              install=steps)
    root = archive.parent / "thing-1.0.0"
    deploy_help = subprocess.run([sys.executable, str(root / "deploy"), "--help"], capture_output=True, text=True)
    assert deploy_help.returncode == 0 and "--config-dir" in deploy_help.stdout and "--install" in deploy_help.stdout
    remove_help = subprocess.run([sys.executable, str(root / "uninstall"), "--help"], capture_output=True, text=True)
    assert remove_help.returncode == 0 and "--delete-data" in remove_help.stdout
    listed = (root / "MANIFEST.sha256").read_text()
    assert "deploy.d/boring_install.py" in listed and "deploy.d/install_steps.py" in listed


class FakeInstances(FakeInstall):
    """Several installations on one host, by name (a Pirate each): HOME/instances/NAME."""

    INSTANCE = installing.Setting("name", "the Pirate's name", pattern=r"[a-z0-9]+")

    def look(self, ctx):
        names = sorted(p.name for p in (ctx.home / "instances").glob("*")) if (ctx.home / "instances").is_dir() else []
        self.calls.append(("look", ctx.instance))
        return installing.Look(ctx.instance in names if ctx.instance else bool(names), list(self.irregular))

    def install(self, ctx, settings):
        self.calls.append(("install", ctx.instance))
        (ctx.home / "instances" / ctx.instance).mkdir(parents=True)
        (ctx.home / "compose.yaml").write_text("services:\n  svc:\n    image: ${BORING_IMAGE}\n")

    def uninstall(self, ctx, delete_data):
        self.calls.append(("uninstall", ctx.instance))
        (ctx.home / "instances" / ctx.instance).rmdir()
        return []


def test_instances_are_installed_one_by_one_and_deployed_together(env):
    tmp, home = env
    fake = FakeInstances()
    url = ("--public-url", "https://pirates.example")
    code, out = run(tmp, home, fake, "--install", *url)
    assert code == 1 and "a first install needs --name" in out
    code, out = run(tmp, home, fake, "--install", "--name", "parrot", *url)
    assert code == 0, out
    code, out = run(tmp, home, fake, version="1.1.0")  # no name: any installed is enough
    assert code == 0 and "7 record  thing 1.1.0 is live" in out
    code, out = run(tmp, home, fake, "--install", "--name", "mo", *url, version="1.2.0")
    assert code == 0, out
    assert ("install", "mo") in fake.calls and sorted(p.name for p in (home / "svc" / "instances").iterdir()) == [
        "mo", "parrot"]
    code, out = run(tmp, home, fake, "--name", "parrot", *url, version="1.3.0")
    assert code == 1 and "settings are for a first install only" in out
    code, out = run(tmp, home, fake, "--name", "Bad!", version="1.3.0")
    assert code == 1 and "is not the Pirate's name" in out


def test_an_instance_is_uninstalled_by_name(env):
    tmp, home = env
    fake = FakeInstances()
    assert run(tmp, home, fake, "--install", "--name", "parrot", "--public-url", "https://pirates.example")[0] == 0
    release = docker(tmp)
    code, out = uninstall(release, fake, str(home / "svc"), "--yes")
    assert code == 1 and "name the one to uninstall: --name" in out
    code, out = uninstall(release, fake, str(home / "svc"), "--yes", "--name", "parrot")
    assert code == 0 and "thing parrot is uninstalled" in out and ("uninstall", "parrot") in fake.calls
    assert (home / "svc" / "DEPLOYED").read_text().splitlines()[-1].split()[1] == "uninstall:parrot"


def test_homes_line_stays_while_an_instance_is_left(env):
    tmp, home = env
    fake = FakeInstances()
    url = ("--public-url", "https://pirates.example")
    assert run(tmp, home, fake, "--install", "--name", "parrot", *url)[0] == 0
    assert run(tmp, home, fake, "--install", "--name", "mo", *url, version="1.1.0")[0] == 0
    release = docker(tmp)
    code, out = uninstall(release, fake, str(home / "svc"), "--yes", "--name", "parrot")
    assert code == 0 and "removed thing" not in out and "thing" in json.loads(boring_deploy.HOMES.read_text())
    code, out = uninstall(release, fake, str(home / "svc"), "--yes", "--name", "mo")
    assert code == 0 and "removed thing from" in out and json.loads(boring_deploy.HOMES.read_text()) == {}


def test_uninstall_refuses_while_a_deploy_runs_and_the_deploy_completes(env):
    tmp, home = env
    fake = FakeInstall()
    assert run(tmp, home, fake, "--install", "--public-url", "https://canvas.example")[0] == 0
    thread, go, result = held_deploy(tmp, home / "svc", docker(tmp, "1.1.0"))
    try:
        code, out = uninstall(docker(tmp), fake, str(home / "svc"), "--yes", "--delete-data")
        assert code == 1 and f"another deploy or uninstall of {home / 'svc'} is in progress" in out
        assert ("uninstall", True) not in fake.calls and (home / "svc" / "compose.yaml").exists()
    finally:
        go.set()
        thread.join(30)
    code, out = result["outcome"]
    assert code == 0, out
    assert (home / "svc" / "release" / "VERSION").read_text() == "1.1.0\n"
    assert uninstall(docker(tmp), fake, str(home / "svc"), "--yes")[0] == 0  # and afterwards it runs


def test_a_deploy_refuses_while_an_uninstall_runs(env):
    tmp, home = env
    inside, go = threading.Event(), threading.Event()

    class Slow(FakeInstall):
        def uninstall(self, ctx, delete_data):
            inside.set()
            assert go.wait(30)
            return super().uninstall(ctx, delete_data)
    fake = Slow()
    assert run(tmp, home, fake, "--install", "--public-url", "https://canvas.example")[0] == 0
    result = {}
    thread = threading.Thread(target=lambda: result.update(
        outcome=uninstall(docker(tmp), fake, str(home / "svc"), "--yes")))
    thread.start()
    try:
        assert inside.wait(30)
        before = snapshot(home / "svc")
        code, out = run(tmp, home, fake, version="1.1.0")
        assert code == 1 and "is in progress" in out and "nothing changed" in out
        assert snapshot(home / "svc") == before
    finally:
        go.set()
        thread.join(30)
    code, out = result["outcome"]
    assert code == 0 and "thing is uninstalled" in out, out
    assert not (home / "svc" / "compose.yaml").exists() and json.loads(boring_deploy.HOMES.read_text()) == {}


def test_homes_json_updates_for_two_components_both_survive(env, monkeypatch):
    """A's read-modify-write is paused after its read; B's waits for it (it does
    not read the old content), so A's line and B's line are both there."""
    tmp, home = env
    boring_deploy.HOMES.write_text(json.dumps({"other": "/opt/other"}))
    a_read, go, b_read = threading.Event(), threading.Event(), threading.Event()
    reading = boring_deploy.homes

    def homes():
        known = reading()
        if threading.current_thread().name == "a":
            a_read.set()
            assert go.wait(30)
        else:
            b_read.set()
        return known
    monkeypatch.setattr(boring_deploy, "homes", homes)

    def record(component):
        ctx = SimpleNamespace(release=SimpleNamespace(component=component), home=home / component, log=print)
        installing._record_home(ctx)
    a = threading.Thread(target=record, args=("canvas",), name="a")
    b = threading.Thread(target=record, args=("knowledge",), name="b")
    a.start()
    try:
        assert a_read.wait(30)
        b.start()
        assert not b_read.wait(0.5)  # B waits for homes.lock, A holds it
    finally:
        go.set()
        a.join(30)
        b.join(30)
    assert b_read.is_set()
    assert json.loads(boring_deploy.HOMES.read_text()) == {
        "other": "/opt/other", "canvas": str(home / "canvas"), "knowledge": str(home / "knowledge")}
    assert_public_file(boring_deploy.HOMES)


# -- sealing what an account leaves behind -------------------------------------

@pytest.fixture
def sealing(monkeypatch, tmp_path):
    """seal() as the test user: root's uid is played by a uid nobody here has, and
    fchown records what it was asked (the test cannot give a folder to root)."""
    owned = {}
    monkeypatch.setattr(installing, "SEAL_OWNER", (0, 0))

    def fchown(fd, uid, gid):
        owned[os.readlink(f"/proc/self/fd/{fd}")] = (uid, gid)
    monkeypatch.setattr(installing.os, "fchown", fchown)
    logged = []
    return SimpleNamespace(owned=owned, ctx=SimpleNamespace(log=logged.append), logged=logged, me=os.getuid())


def test_seal_gives_each_kept_folder_to_root_with_mode_0700(sealing, tmp_path):
    state, home = tmp_path / "state", tmp_path / "home"
    for folder in (state, home):
        folder.mkdir(mode=0o750)
        (folder / "data").write_text("kept")
    installing.seal(sealing.ctx, [state, home, tmp_path / "gone"], sealing.me)
    assert sealing.owned == {str(state): (0, 0), str(home): (0, 0)}
    for folder in (state, home):
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700 and (folder / "data").read_text() == "kept"


@pytest.mark.parametrize("kind", ["symlink", "file", "foreign"])
def test_seal_refuses_before_any_change(sealing, tmp_path, monkeypatch, kind):
    good, odd = tmp_path / "good", tmp_path / "odd"
    good.mkdir(mode=0o750)
    os.chmod(good, 0o750)
    if kind == "symlink":
        (tmp_path / "elsewhere").mkdir()
        os.chmod(tmp_path / "elsewhere", 0o755)  # not the umask's
        odd.symlink_to(tmp_path / "elsewhere")
    elif kind == "file":
        odd.write_text("")
    else:
        odd.mkdir()
    uid = sealing.me if kind != "foreign" else sealing.me + 1  # odd is then another account's
    folders = [good, odd] if kind != "foreign" else [odd]
    with pytest.raises(boring_deploy.Failure, match="not sealed, the account is kept"):
        installing.seal(sealing.ctx, folders, uid)
    assert sealing.owned == {} and stat.S_IMODE(good.stat().st_mode) == 0o750
    if kind == "symlink":
        assert stat.S_IMODE((tmp_path / "elsewhere").stat().st_mode) == 0o755


def test_seal_refuses_a_mount_point_and_leaves_sealed_folders(sealing, tmp_path, monkeypatch):
    folder = tmp_path / "state"
    folder.mkdir()
    monkeypatch.setattr(installing, "_mount_points", lambda: {str(folder)})
    with pytest.raises(boring_deploy.Failure, match="mount point"):
        installing.seal(sealing.ctx, [folder], sealing.me)
    monkeypatch.setattr(installing, "_mount_points", lambda: set())
    monkeypatch.setattr(installing, "SEAL_OWNER", (sealing.me, os.getgid()))  # as if root's already
    installing.seal(sealing.ctx, [folder], 4242)  # a rerun: nothing to do, nothing refused
    assert sealing.owned == {}


def test_seal_check_changes_nothing_and_names_what_is_left(sealing, tmp_path):
    """The check an uninstall runs in its preflight, and seal_folders with a plain log."""
    owned, gone = tmp_path / "owned", tmp_path / "gone"
    owned.mkdir(mode=0o750)
    os.chmod(owned, 0o750)
    assert [folder for folder, _ in installing.seal_check([owned, gone], sealing.me)] == [owned]
    assert sealing.owned == {} and stat.S_IMODE(owned.stat().st_mode) == 0o750
    with pytest.raises(boring_deploy.Failure, match="belongs to uid"):
        installing.seal_check([owned], sealing.me + 1)
    lines = []
    installing.seal_folders([owned], sealing.me, lines.append)
    assert sealing.owned == {str(owned): (0, 0)} and lines == [
        f"sealed {owned}: root's, 0700 (inside, files keep uid {sealing.me})"]

def test_a_relative_home_is_recorded_absolute(env, monkeypatch):
    tmp, home = env
    monkeypatch.chdir(home)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = boring_deploy.main(docker(tmp, "1.0.0"), Steps(), ["svc", "--install", "--public-url",
                                  "https://canvas.example"], install=FakeInstall())
    assert code == 0, out.getvalue() + err.getvalue()
    assert json.loads(boring_deploy.HOMES.read_text()) == {"thing": str(home / "svc")}
