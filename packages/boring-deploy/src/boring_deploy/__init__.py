"""Install a release of one Pond component into its HOME, or roll back to one.

A release is a directory (an unpacked artifact) with

    RELEASE.json      component, version, runtime, default_home, requires,
                      data_generation, upgrades_from (and runtime details)
    MANIFEST.sha256   every file of the release, `<sha256>  <path>`
    deploy            the launcher: this engine plus the component's steps

and runs `sudo <release>/deploy [HOME] [--check] [--backup | --no-backup]`.
One deploy or uninstall of a HOME at a time (operation_lock: a second one
refuses). Steps, stopping at the first failure:

  0. install?  only for a component with install steps (install.py): reality
               says installed, not installed (install first, asked) or irregular
  1. verify    files against the manifest; HOME belongs to this component;
               `requires` met by what is installed; data generation readable
  2. prepare   venv: HOME/releases/<version>/ with app/ installed;
               docker: HOME/release.new/ and the image built
  3. check     the component's own check with the new release, nothing live
  -  backup    only when the data generation changes (asked, or the flag)
  4. switch    venv: previous <- current, current <- new, restart the units
               that run (a stopped unit stays stopped);
               docker: boring-image.env BORING_IMAGE <- new image, compose up -d (an
               update leaves a stopped stack stopped; a first install starts it;
               only the compose services units(ctx) names, else the whole stack)
  5. start     wait until running (nothing to wait for when all were stopped)
  6. smoke     the component's own end-to-end check (skipped when stopped)
  7. record    append to HOME/DEPLOYED, print the component's way back

Steps 0-3 do not change what runs; step 2 writes the new release into HOME
beside the running one (and builds its image), and an approved first install
(step 0) changes the host. `--check` is read-only: steps 0 and 1 (the
unpacked release and the host), then it stops; it writes nothing, not even a
lock file. Nothing is rolled back automatically. Rollback is the previous
release's own deploy: HOME/previous/deploy (venv) or
HOME/release.previous/deploy (docker).

The component supplies `steps`, a module with optional functions
check(ctx), smoke(ctx), units(ctx) -> [unit, ...] (venv: the units an update
restarts when they run; docker: the compose services the deploy owns, started
and waited for, the whole stack when none are named), healthy(ctx) -> bool | None (the running release
passes its own checks, asked before the switch; a healthy one always becomes
previous), preflight(ctx) -> list[str] (read-only problems before any install),
data_generation(ctx) -> int | None, backup(ctx, directory) -> Path,
way_back(ctx, previous) -> str | None (read-only recovery guidance for the
retained release; None keeps its normal deploy command),
completion(ctx) -> str | None (read-only completion wording after success;
None keeps the normal live/stopped message; it never skips start or smoke),
lock(ctx) -> a context manager held from step 1 to the record (a component's
own lock, taken after the operation lock: see the README's lock order).
Standard library only: it runs on the host's python3 (3.12 or newer; the
launcher refuses older) before anything is installed.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import hashlib
import grp
import json
import os
import platform
import pwd
import re
import shutil
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

HOMES = Path("/etc/boring-pond/homes.json")
LOCKS = Path("/run/boring-pond")  # root's: the operation locks and homes.lock (gone at reboot, as are the holders)
GENERATED = {"app", "bin", "__pycache__"}  # made by prepare in a venv release directory
# The supported host floor of the Linux x86_64 artifacts we build.
LINUX_GLIBC_FLOOR = (2, 39)
WAIT_SECONDS = 120
COMPLETE = ".installed"  # app/ is used only once this marker exists


class Failure(Exception):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


@dataclass(frozen=True)
class AccountIdentity:
    """NSS-assigned IDs retained by the component to detect account replacement."""
    uid: int
    gid: int

    def __post_init__(self) -> None:
        if type(self.uid) is not int or type(self.gid) is not int or min(self.uid, self.gid) <= 0:
            raise Failure("restricted account identity needs positive uid and gid")


def system_account(name: str, home: Path, comment: str,
                   expected: AccountIdentity | None = None) -> AccountIdentity:
    """Observe one restricted identity; this does not authorize adopting it.

    The component owns whether an existing account belongs to its installation.
    Pass its retained IDs on later operations. No shadow/password data is read.
    """
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", name) or not home.is_absolute():
        raise Failure("restricted account needs a safe name and absolute home")
    try:
        user, group = pwd.getpwnam(name), grp.getgrnam(name)
    except KeyError:
        raise Failure(f"restricted account {name}: identity not observed") from None
    identity = AccountIdentity(user.pw_uid, user.pw_gid)
    if (user.pw_name != name or user.pw_dir != str(home) or user.pw_shell != "/usr/sbin/nologin"
            or user.pw_gecos != comment or group.gr_name != name or group.gr_gid != identity.gid
            or set(os.getgrouplist(name, identity.gid)) != {identity.gid}
            or set(group.gr_mem) - {name}
            or any(other.pw_gid == identity.gid and other.pw_name != name for other in pwd.getpwall())
            or (expected is not None and identity != expected)):
        raise Failure(f"restricted account {name}: identity or membership changed; retained")
    return identity


def create_system_account(name: str, home: Path, comment: str,
                          run: Callable[..., Any]) -> AccountIdentity:
    """Create a fresh system identity with private group and no login shell.

    Never adopts names or paths. The component creates its own directories after
    observing the assigned IDs; no home, group or account is ever deleted here.
    run is the deployment Context's checked child runner.
    """
    if (not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", name) or not home.is_absolute()
            or not isinstance(comment, str) or not comment or any(c in comment for c in "\n\r:\0")):
        raise Failure("restricted account needs a safe name, absolute home and comment")
    for lookup in (pwd.getpwnam, grp.getgrnam):
        try:
            lookup(name)
        except KeyError:
            continue
        raise Failure(f"restricted account {name}: name already exists; not adopted")
    if os.path.lexists(home):
        raise Failure(f"restricted account {name}: home already exists; retained")
    run("useradd", "--system", "--user-group", "--no-create-home",
        "--home-dir", str(home), "--shell", "/usr/sbin/nologin", "--comment", comment, name)
    return system_account(name, home, comment)


def lock_system_account(name: str, home: Path, comment: str, expected: AccountIdentity,
                        run: Callable[..., Any]) -> None:
    """Recheck identity, then lock password and expire the account; keep all files.

    Expiry is needed because password locking alone does not revoke public keys.
    This does not stop existing sessions. The component owns SSH key revocation
    and checking that its tunnel has closed before removing service grants.
    """
    if not isinstance(expected, AccountIdentity):
        raise Failure("locking a restricted account needs its retained identity")
    system_account(name, home, comment, expected)
    run("usermod", "--lock", "--expiredate", "1970-01-02", name)


# Root children use the host's tool directories, never the operator's PATH.
SYSTEM_TOOL_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
_PYTHON_PROGRAM = re.compile(r"python(?:[0-9]+(?:\.[0-9]+)?)?")


def _root_argv(command: Sequence[str | os.PathLike[str]]) -> list[str]:
    if not command:
        raise Failure("a root child needs an executable")
    program, *arguments = map(os.fspath, command)
    if not os.path.isabs(program):
        if "/" in program:
            raise Failure("root child executable must be absolute: " + program)
        found = shutil.which(program, path=SYSTEM_TOOL_PATH)
        if found is None:
            raise Failure("host tool not found in system directories: " + program)
        program = found
    if _PYTHON_PROGRAM.fullmatch(Path(program).name):
        while arguments and arguments[0] in {"-I", "-B"}:
            arguments.pop(0)
        return [program, "-I", "-B", *arguments]
    # Console scripts are Python children too. Isolate their interpreter rather
    # than trusting PYTHONPATH just because the script path is absolute.
    try:
        with open(program, "rb") as source:
            first = source.readline(4096)
    except OSError:
        first = b""  # the process launcher reports a missing/unreadable executable
    if first.startswith(b"#!"):
        interpreter = shlex.split(first[2:].decode("utf-8").strip())
        if interpreter and Path(interpreter[0]).name == "env":
            interpreter = interpreter[1:]
            if interpreter[:1] == ["-S"]:
                interpreter = interpreter[1:]
        if interpreter and _PYTHON_PROGRAM.fullmatch(Path(interpreter[0]).name):
            return _root_argv([*interpreter, program, *arguments])
    return [program, *arguments]


def root_child(command: Sequence[str | os.PathLike[str]], *, user: str | None = None,
               spawn: Callable[..., Any] | None = None, **options: Any) -> Any:
    """Launch a root-controlled child with isolated Python, system tools and cwd /.

    The caller owns capture, deadlines and cleanup. Pass subprocess.Popen as
    spawn when that owner needs a process handle instead of a completed result.
    A user switch wraps the already-normalized child, not the other way round.
    """
    if Path(options.pop("cwd", "/")) != Path("/"):
        raise Failure("root child working directory must be /")
    arguments = _root_argv(command)
    if user is not None:
        runuser = _root_argv(["runuser"])
        arguments = [*runuser, "-u", user, "--", *arguments]
    environment = {**os.environ, **(options.pop("env", None) or {})}
    environment["PATH"] = SYSTEM_TOOL_PATH
    return (spawn or subprocess.run)(arguments, cwd="/", env=environment, **options)


def _input_info(fd: int, path: Path, owner: int, *, ancestor: bool = False) -> dict[str, Any]:
    info = os.fstat(fd)
    directory = stat.S_ISDIR(info.st_mode)
    if not directory and not stat.S_ISREG(info.st_mode):
        raise Failure(f"unsafe operator input {path}: needs a regular file or directory")
    if info.st_uid not in ({0, owner} if ancestor else {owner}):
        raise Failure(f"unsafe operator input {path}: owner uid {info.st_uid}, expected {owner}")
    # A root-owned sticky ancestor (e.g. /tmp) cannot replace a root-owned child.
    sticky_parent = ancestor and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
    if info.st_mode & 0o022 and not sticky_parent:
        raise Failure(f"unsafe operator input {path}: mode {stat.S_IMODE(info.st_mode):04o} permits another writer")
    if not directory and info.st_nlink > 1:
        raise Failure(f"unsafe operator input {path}: multiple hard links")
    return dict(path=str(path), uid=info.st_uid, gid=info.st_gid,
                mode=stat.S_IMODE(info.st_mode), kind="directory" if directory else "file")


@dataclass
class RootInput:
    """An opened operator input. Metadata never includes its content."""
    path: Path
    fd: int = field(repr=False)
    owner: int = field(repr=False)
    metadata: list[dict[str, Any]]

    def describe(self) -> list[str]:
        return [f"operator input {row['path']}: uid {row['uid']}, gid {row['gid']}, mode {row['mode']:04o}"
                for row in self.metadata]

    def _source(self):
        info = _input_info(self.fd, self.path, self.owner)
        if info["kind"] != "file":
            raise Failure(f"operator input {self.path}: cannot read a directory as a file")
        stream = os.fdopen(os.dup(self.fd), "rb")
        stream.seek(0)
        return stream

    def read_bytes(self) -> bytes:
        with self._source() as stream:
            return stream.read()

    def read_text(self, encoding: str = "utf-8") -> str:
        return self.read_bytes().decode(encoding)

    def copy_to(self, target: Path) -> None:
        with self._source() as source:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            with os.fdopen(fd, "wb") as output:
                shutil.copyfileobj(source, output)


@contextlib.contextmanager
def root_input(path: Path | str, *, directory: bool = False, owner: int = 0) -> Iterator[RootInput]:
    """Open a root-owned input without following any component or tree symlink.

    owner permits synthetic ordinary-user test trees; root installers keep 0.
    The opened descriptor remains the read/copy source until the context ends.
    """
    path = Path(path)
    if not path.is_absolute():
        raise Failure(f"operator input {path}: use an absolute path")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    with contextlib.ExitStack() as opened:
        def acquire(name, *, parent=None, is_directory=False):
            fd = os.open(name, flags | (os.O_DIRECTORY if is_directory else 0), dir_fd=parent)
            opened.callback(os.close, fd)
            return fd
        try:
            fd = acquire("/", is_directory=True)
            current = Path("/")
            _input_info(fd, current, owner, ancestor=True)
            for index, name in enumerate(path.parts[1:]):
                last = index == len(path.parts) - 2
                current = current / name
                fd = acquire(name, parent=fd, is_directory=not last or directory)
                _input_info(fd, current, owner, ancestor=not last)
            metadata = [_input_info(fd, path, owner)]
            if (metadata[0]["kind"] == "directory") != directory:
                raise Failure(f"operator input {path}: expected {'directory' if directory else 'file'}")
            def scan(parent, folder):
                for name in sorted(os.listdir(parent)):
                    child = folder / name
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                        raise Failure(f"unsafe operator input {child}: links and special files are refused")
                    child_fd = os.open(name, flags | (os.O_DIRECTORY if stat.S_ISDIR(info.st_mode) else 0),
                                       dir_fd=parent)
                    try:
                        row = _input_info(child_fd, child, owner)
                        metadata.append(row)
                        if row["kind"] == "directory":
                            scan(child_fd, child)
                    finally:
                        os.close(child_fd)
            if directory:
                scan(fd, path)
        except OSError as error:
            raise Failure(f"unsafe operator input {current}: {error.strerror}") from error
        yield RootInput(path, fd, owner, metadata)


@dataclass
class Release:
    directory: Path
    component: str
    version: str
    runtime: str
    default_home: str
    requires: dict[str, str] = field(default_factory=dict)
    data_generation: int | None = None
    upgrades_from: int | None = None
    details: dict[str, Any] = field(default_factory=dict)
    glibc_min: str | None = None
    architecture: str | None = None

    @classmethod
    def load(cls, directory: Path) -> "Release":
        raw = json.loads((directory / "RELEASE.json").read_text())
        known = {"component", "version", "runtime", "default_home", "requires", "data_generation", "upgrades_from", "glibc_min",
                 "architecture"}
        floor = raw.get("glibc_min")
        if "glibc_min" in raw and (not isinstance(floor, str) or not re.fullmatch(r"[0-9]+\.[0-9]+", floor)):
            raise Failure("RELEASE.json: glibc_min must be a major.minor version string")
        architecture = raw.get("architecture")
        if "architecture" in raw and (not isinstance(architecture, str) or not architecture):
            raise Failure("RELEASE.json: architecture must be a non-empty string")
        if raw.get("runtime") not in ("venv", "docker", "files"):
            raise Failure("RELEASE.json: runtime must be venv, docker or files")
        return cls(directory, raw["component"], raw["version"], raw["runtime"], raw["default_home"],
                   dict(raw.get("requires", {})), raw.get("data_generation"), raw.get("upgrades_from"),
                   {k: v for k, v in raw.items() if k not in known}, floor, architecture)


@dataclass
class Context:
    release: Release
    home: Path
    installed: str | None
    log: Callable[[str], None]
    new: Path | None = None       # the prepared release in HOME
    image: str | None = None      # docker: the image of this release
    running_healthy: bool | None = None  # the running release's own checks, before the switch
    installing: bool = False      # a first install has started changing the host
    instance: str | None = None   # install steps with INSTANCE: the one named (--NAME), if any
    start: list[str] = field(default_factory=list)      # units an install made: started even though inactive
    restarted: list[str] = field(default_factory=list)  # units the switch restarted (they ran, or start)
    stopped: list[str] = field(default_factory=list)    # what was stopped and stays stopped (units, a stack)
    not_started: bool = False     # the switch left the component stopped: no start, no smoke
    # What a step changed on the host before the switch (the steps' own words),
    # so a failure before the switch is not reported as "nothing changed".
    changed: list[str] = field(default_factory=list)

    def run(self, *command: str, user: str | None = None, check: bool = True,
            capture: bool = True, env: dict[str, str] | None = None,
            interactive: bool = False) -> subprocess.CompletedProcess[str]:
        """Run one step's command. Its stdin is /dev/null unless `interactive`:
        the engine asks the operator itself (input()), a child never reads the
        terminal. A child that inherits a terminal's stdin can block forever:
        `docker compose run` keeps stdin attached and, run from an interactive
        shell, was stopped by the terminal (SIGTTIN)."""
        result = root_child(command, user=user, text=True, capture_output=capture,
                            stdin=None if interactive else subprocess.DEVNULL, env=env)
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()[-5:] if capture else []
            raise Failure(f"{' '.join(command)} exited {result.returncode}" + "".join("\n  " + l for l in detail))
        return result


# -- versions ---------------------------------------------------------------

def version_key(version: str) -> tuple[int, ...]:
    """Release part, then pre-releases (dev < rc) before the release itself."""
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.(\d+))?(?:[-.]?(dev|rc)\.?(\d+))?", version.strip())
    if not match:
        raise Failure(f"not a version: {version}")
    major, minor, patch, kind, number = match.groups()
    pre = (2, 0) if kind is None else ({"dev": 0, "rc": 1}[kind], int(number))
    return (int(major), int(minor), int(patch or 0), *pre)


def satisfies(version: str, spec: str) -> bool:
    for clause in filter(None, (c.strip() for c in spec.split(","))):
        match = re.match(r"(>=|<=|==|<|>)\s*(.+)", clause)
        if not match:
            raise Failure(f"not a version requirement: {clause}")
        op, wanted = match.groups()
        a, b = version_key(version), version_key(wanted)
        if not {">=": a >= b, "<=": a <= b, "==": a == b, "<": a < b, ">": a > b}[op]:
            return False
    return True


# -- runtimes ----------------------------------------------------------------

def installed_version(home: Path, runtime: str) -> str | None:
    marker = home / ("release" if runtime == "docker" else "current") / "VERSION"
    return marker.read_text().strip() if marker.is_file() else None


def homes() -> dict[str, str]:
    return json.loads(HOMES.read_text()) if HOMES.is_file() else {}


# -- locks -------------------------------------------------------------------
#
# Order: a HOME's operation lock first, then a component's own lock (the
# Crocodile's site lock, steps.lock), then homes.lock (innermost, held only for
# the read-modify-write of homes.json). Operation and component locks refuse
# at once; homes.lock waits, being short. Nothing that holds a later lock ever
# takes an earlier one.

def _lock_folder(create: bool) -> Path | None:
    """LOCKS, a folder of ours (root's on a host) that nobody else can write."""
    if create:
        LOCKS.mkdir(mode=0o755, exist_ok=True)
    try:
        info = LOCKS.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise Failure(f"unsafe lock folder {LOCKS}: it must be a folder of root's that only root can write")
    return LOCKS


def _open_lock(path: Path, create: bool) -> int | None:
    """The lock file's descriptor (None: it does not exist and create is off)."""
    flags = os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_RDWR | os.O_CREAT if create else os.O_RDONLY)
    try:
        fd = os.open(path, flags, 0o600)
    except FileNotFoundError:
        return None
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
            or info.st_mode & 0o077):
        os.close(fd)
        raise Failure(f"unsafe lock file {path}: it must be a private regular file of root's")
    return fd


def lock_path(home: Path) -> Path:
    """HOME's operation lock: its real path, readable and hashed (one lock per
    folder, whatever the spelling or symlink that names it)."""
    real = os.path.realpath(home)
    readable = re.sub(r"[^A-Za-z0-9_.-]+", "-", real.strip("/"))[:80] or "root"
    return LOCKS / f"home-{readable}-{hashlib.sha256(real.encode()).hexdigest()[:12]}.lock"


@contextlib.contextmanager
def operation_lock(home: Path, *, shared: bool = False) -> Iterator[None]:
    """One deploy or uninstall of HOME at a time, from before the state it
    decides on is read to after its record. It never waits: a second operation
    refuses and changes nothing. `shared` (deploy --check) writes nothing: it
    holds the lock file only when one exists, alongside other checks, and
    refuses while a deploy or uninstall holds it."""
    folder = _lock_folder(create=not shared)
    fd = None if folder is None else _open_lock(lock_path(home), create=not shared)
    try:
        if fd is not None:
            try:
                fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Failure(f"another deploy or uninstall of {home} is in progress ({lock_path(home)} is held)",
                              "let it finish, then run this again") from None
        yield
    finally:
        if fd is not None:
            os.close(fd)


@contextlib.contextmanager
def homes_update() -> Iterator[dict[str, str]]:
    """homes.json's read-modify-write, one at a time on the host (LOCKS/homes.lock;
    waits, it is short): yields its content; the caller changes the dict, and
    it is written whole when it differs, on leaving without an exception."""
    _lock_folder(create=True)
    fd = _open_lock(LOCKS / "homes.lock", create=True)
    assert fd is not None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        known = homes()
        before = dict(known)
        yield known
        if known != before:
            HOMES.parent.mkdir(parents=True, exist_ok=True)
            replace_public(HOMES, json.dumps(known, indent=2, sort_keys=True) + "\n")
    finally:
        os.close(fd)


def last_good(home: Path) -> str | None:
    """The version of the last deploy that completed (DEPLOYED's last line)."""
    log = home / "DEPLOYED"
    lines = log.read_text().splitlines() if log.is_file() else []
    return lines[-1].split()[3] if lines else None


def worth_keeping(home: Path, release: Path, healthy: bool | None = None) -> bool:
    """Whether the running release becomes `previous`. Reality first: a release
    whose own checks pass right before the switch (the component's `healthy`
    step) is kept. Otherwise it is kept if it was the last good deploy (DEPLOYED's
    last line, or no DEPLOYED: the first deploy over an older installation); a
    release whose deploy failed after its switch is dropped. The record can add
    a rollback target; it never removes a healthy one."""
    if healthy:
        return True
    good = last_good(home)
    version = release / "VERSION"
    return good is None or (version.is_file() and version.read_text().strip() == good)


def relink(link: Path, target: Path) -> None:
    temporary = link.with_name(link.name + ".new")
    if temporary.is_symlink() or temporary.exists():
        temporary.unlink()
    temporary.symlink_to(os.path.relpath(target, link.parent))
    os.replace(temporary, link)


class Venv:
    """HOME/releases/<version>/ with app/ (a standalone Python with the
    application) and bin -> app/bin; HOME/current and HOME/previous link to
    releases. Units name HOME/current/bin/..., so a deploy never edits them."""
    PREVIOUS = "previous"
    START = "start it with systemctl"

    def check_host(self, ctx: Context) -> None:
        """Step 1, read-only: what the switch needs on the host (nothing more here)."""

    def _prepare_files(self, ctx: Context) -> Path:
        release = ctx.release
        target = ctx.home / "releases" / release.version
        if release.directory.resolve() != target.resolve():
            if target.exists():
                if (target / "MANIFEST.sha256").read_bytes() != (release.directory / "MANIFEST.sha256").read_bytes():
                    raise Failure(f"{target} holds version {release.version} with different files",
                                  "a version is never rebuilt: tag a new one")
                verify_files(target)  # reused: it may be running, so a mismatch is reported, never removed
                readable_by_all(target)  # a copy left by an older deploy may still carry its source's modes
            else:
                (ctx.home / "releases").mkdir(parents=True, exist_ok=True)
                partial = target.with_name(target.name + ".partial")
                shutil.rmtree(partial, ignore_errors=True)
                top = release.directory.resolve()
                shutil.copytree(top, partial, symlinks=True,
                                ignore=lambda d, names: [n for n in names if Path(d) == top and n in GENERATED])
                verify_copy(partial)
                readable_by_all(partial)
                partial.rename(target)
        ctx.new = target
        return target

    def prepare(self, ctx: Context) -> None:
        target = self._prepare_files(ctx)
        if not (target / "app" / COMPLETE).exists():
            install_app(ctx, target)
        else:
            link = target / "bin"
            if not os.path.lexists(link):
                link.symlink_to("app/bin")  # Repair the old marker-before-link interruption.
            elif not link.is_symlink() or os.readlink(link) != "app/bin":
                raise Failure(f"{target}: completed runtime has an unexpected bin link",
                              "inspect the release before retrying; bin must link to app/bin")
        ctx.log(f"prepared {target}")

    def switch(self, ctx: Context, units: list[str]) -> None:
        """An update restarts the units that run and leaves a stopped (inactive)
        unit stopped: `systemctl disable --now` holds across updates. A failed
        unit is restarted (it was meant to run), and so is a unit the install
        just made (ctx.start). Units, none restarted: ctx.not_started."""
        assert ctx.new is not None
        states = {u: ctx.run("systemctl", "is-active", u, check=False).stdout.strip() for u in units}
        ctx.restarted = [u for u in units if states[u] != "inactive" or u in ctx.start]
        ctx.stopped = [u for u in units if u not in ctx.restarted]
        current, previous = ctx.home / "current", ctx.home / "previous"
        old = current.resolve() if current.is_symlink() else None
        if old is not None and old != ctx.new.resolve() and worth_keeping(ctx.home, old, ctx.running_healthy):
            relink(previous, old)
        relink(current, ctx.new)
        keep = {ctx.new.resolve(), previous.resolve() if previous.is_symlink() else None}
        for name in list((ctx.home / "releases").iterdir()):
            if name.resolve() not in keep:
                shutil.rmtree(name)
        if units:
            ctx.run("systemctl", "daemon-reload")
        if ctx.restarted:
            ctx.run("systemctl", "restart", *ctx.restarted)
        ctx.not_started = bool(units) and not ctx.restarted

    def wait(self, ctx: Context, units: list[str]) -> None:
        deadline = time.monotonic() + WAIT_SECONDS
        while True:
            states = {u: ctx.run("systemctl", "is-active", u, check=False).stdout.strip() for u in units}
            if all(s == "active" for s in states.values()):
                return
            if time.monotonic() > deadline or any(s == "failed" for s in states.values()):
                raise Failure("not running: " + ", ".join(f"{u} {s}" for u, s in states.items() if s != "active"),
                              "journalctl -u <unit> -n 50")
            time.sleep(1)



class Files(Venv):
    """Configuration-only releases: verified code, no application runtime or daemon."""
    START = "configuration only; no application to start"

    def prepare(self, ctx: Context) -> None:
        ctx.log(f"prepared {self._prepare_files(ctx)} (configuration only)")


def readable_by_all(root: Path) -> None:
    """The services run as their own accounts, not root: every directory and
    executable 0755, every other file 0644. copytree keeps the source's modes,
    and a release unpacked under a private umask (0027 in a builder's home) made
    the controller unexecutable (203/EXEC)."""
    for path in [root, *root.rglob("*")]:
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() or os.access(path, os.X_OK) else 0o644)


def install_app(ctx: Context, target: Path) -> None:
    """Unpack the release's standalone Python into app/ and install the wheels
    into it offline, hash-checked. Done here, not at build time, because
    console scripts carry the absolute path of their interpreter."""
    details = ctx.release.details
    app = target / "app"
    shutil.rmtree(app, ignore_errors=True)
    with tempfile.TemporaryDirectory(dir=target) as tmp:
        shutil.unpack_archive(target / details["python"], tmp, filter="data")
        (top,) = Path(tmp).iterdir()  # python-build-standalone: one top directory
        top.rename(app)
    python = str(app / "bin" / "python3")
    wheels = target / "wheels"
    pip = (python, "-I", "-m", "pip", "install", "--quiet", "--no-index", "--no-deps", "--no-compile")
    ctx.run(*pip, "--require-hashes", "--find-links", str(wheels), "-r", str(wheels / "requirements.lock"))
    for wheel in details.get("app_wheels", []):
        ctx.run(*pip, str(wheels / wheel))
    ctx.run(python, "-I", "-m", "pip", "check")
    # Compile once, as root: the services run as their own accounts with /opt
    # read-only and would otherwise compile every module at each start.
    ctx.run(python, "-I", "-c", "import compileall, sys, sysconfig; "
            "sys.exit(not compileall.compile_dir(sysconfig.get_path('purelib'), quiet=1, workers=0))")
    readable_by_all(app)
    link = target / "bin"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to("app/bin")
    (app / COMPLETE).write_text(ctx.release.version + "\n")  # Publish completion last.


class Docker:
    """HOME/compose.yaml (the operator's, image: ${BORING_IMAGE}), HOME/boring-image.env,
    HOME/release/ and HOME/release.previous/.

    `units` are compose service names: the part of the stack this component's
    deploy owns (a front end beside backends it does not build, say). Whether it
    runs, `up -d` and the wait apply to those services only (with their
    `depends_on`, as Compose starts them); other services are the operator's,
    neither started nor waited for. No units: the whole stack, as before.
    check_units refuses, before the switch, a name that is not a service of the
    stack and an unnamed service that uses ${BORING_IMAGE} (it would keep the old
    image while boring-image.env names the new one). The wait covers the named services
    only, not their `depends_on`."""
    MARKER = "boring-deploy-marker:units"  # stands for ${BORING_IMAGE} when the stack is rendered
    PREVIOUS = "release.previous"

    def check_host(self, ctx: Context) -> None:
        """Step 1, read-only: the operator's compose.yaml names its image as the deploy sets it."""
        compose = ctx.home / "compose.yaml"
        if "${BORING_IMAGE}" not in compose.read_text():
            raise Failure(f"{compose} does not name its image as ${{BORING_IMAGE}}",
                          "once: set `image: ${BORING_IMAGE}` in compose.yaml")

    def prepare(self, ctx: Context) -> None:
        release, home = ctx.release, ctx.home
        # A tag is immutable even when the retained copy is the rollback target.
        for retained in (home / "release", home / "release.previous"):
            if (retained / "VERSION").is_file() and (retained / "VERSION").read_text().strip() == release.version:
                if (retained / "MANIFEST.sha256").read_bytes() != (release.directory / "MANIFEST.sha256").read_bytes():
                    raise Failure(f"{retained} holds version {release.version} with different files",
                                  "a version is never rebuilt: tag a new one")
                verify_files(retained)
        new = home / "release.new"
        shutil.rmtree(new, ignore_errors=True)
        # Fresh mtimes (shutil.copy: content and mode, not times). The release's
        # files all carry the artifact's fixed mtime and every build's context is
        # this same path; BuildKit keeps its synced copy of a context per path and
        # takes a file with unchanged size and mtime as unchanged, so a same-size
        # requirements.txt of a newer release was built from the older one's copy.
        shutil.copytree(release.directory.resolve(), new, symlinks=True, copy_function=shutil.copy)
        verify_copy(new)
        ctx.new = new
        ctx.image = f"{release.details['image']}:{release.version}"
        ctx.run("docker", "build", "--quiet", "-t", ctx.image, str(new / release.details.get("context", "image")))
        ctx.log(f"built {ctx.image}")

    def compose(self, ctx: Context, *args: str, image: str | None = None, **kw: Any) -> subprocess.CompletedProcess[str]:
        env = {"BORING_IMAGE": image} if image else None
        return ctx.run(*compose_command(ctx.home, *args), env=env, **kw)

    def check_units(self, ctx: Context, units: list[str]) -> None:
        """Step 4, before the switch writes anything: the named services exist,
        and every service that uses ${BORING_IMAGE} is among them."""
        if not units:
            return
        known = self.compose(ctx, "config", "--services", image=self.MARKER).stdout.split()
        unknown = [u for u in units if u not in known]
        if unknown:
            raise Failure(f"units names no service of {ctx.home / 'compose.yaml'}: {', '.join(unknown)}",
                          f"its services: {', '.join(known) or 'none'}")
        rendered = json.loads(self.compose(ctx, "config", "--format", "json", image=self.MARKER).stdout)
        sharing = sorted(name for name, service in rendered.get("services", {}).items()
                         if isinstance(service, dict) and service.get("image") == self.MARKER and name not in units)
        if sharing:
            raise Failure(f"{', '.join(sharing)} also use ${{BORING_IMAGE}} but the deploy starts only "
                          f"{', '.join(units)}: they would keep the old image while boring-image.env names the new one",
                          "name them in the component's units, or give them an image of their own")

    def ps(self, ctx: Context, check: bool = False, services: tuple[str, ...] | list[str] = ()) -> list[dict[str, Any]]:
        """The containers of `services` (all: the stack), running or not
        (`compose ps --all --format json`: one object per line, or one array in
        older Compose releases)."""
        text = self.compose(ctx, "ps", "--all", "--format", "json", *services, check=check).stdout.strip()
        if text.startswith("["):
            return [r for r in json.loads(text) if isinstance(r, dict)]
        return [json.loads(line) for line in text.splitlines() if line.strip().startswith("{")]

    def running(self, ctx: Context, services: tuple[str, ...] | list[str] = ()) -> bool:
        """Whether the stack runs: a container running or restarting (meant to
        run). None, or only exited/created ones: it is stopped (`compose stop`,
        `compose down`). The family's stacks use `restart: unless-stopped`, so a
        crashed container is restarting, not exited: exited means an operator
        stopped it. A failing `compose ps` is a failure, never a guess.
        With `services`, only theirs count."""
        return any(r.get("State") in ("running", "restarting") for r in self.ps(ctx, check=True, services=services))

    def switch(self, ctx: Context, units: list[str]) -> None:
        """The new image goes into boring-image.env either way. An update leaves a stopped
        stack stopped, as the venv runtime leaves a stopped unit: no `compose
        up`, so the next start runs the new image. A first install (nothing
        installed) starts it. Decided before anything changes."""
        home = ctx.home
        assert ctx.new is not None and ctx.image is not None
        ctx.not_started = ctx.installed is not None and not self.running(ctx, units)
        replace_public(home / IMAGE_ENV, f"BORING_IMAGE={ctx.image}\n")
        migrate_image_env(home / ".env")
        if (home / "release").exists():
            same_version = (home / "release" / "VERSION").read_text().strip() == ctx.release.version
            if not same_version and worth_keeping(home, home / "release", ctx.running_healthy):
                shutil.rmtree(home / "release.previous", ignore_errors=True)
                (home / "release").rename(home / "release.previous")
            else:
                shutil.rmtree(home / "release")
        ctx.new.rename(home / "release")
        ctx.new = home / "release"
        if ctx.not_started:
            ctx.stopped = [f"the compose stack {home / 'compose.yaml'}"
                           + (f" ({', '.join(units)})" if units else "")]
        else:
            self.compose(ctx, "up", "-d", *units)
            ctx.restarted = list(units)  # what the wait asks about (none: the whole stack)
        self.prune(ctx)

    def prune(self, ctx: Context) -> None:
        """Remove this component's images other than current and previous."""
        name = ctx.release.details["image"]
        keep = {ctx.release.version}
        if (ctx.home / "release.previous" / "VERSION").is_file():
            keep.add((ctx.home / "release.previous" / "VERSION").read_text().strip())
        tags = ctx.run("docker", "image", "ls", name, "--format", "{{.Tag}}", check=False).stdout.split()
        for tag in tags:
            if tag not in keep and tag != "<none>":
                ctx.run("docker", "image", "rm", f"{name}:{tag}", check=False)

    def wait(self, ctx: Context, units: list[str]) -> None:
        deadline = time.monotonic() + WAIT_SECONDS
        while True:
            rows = self.ps(ctx, services=units)
            bad = [r for r in rows if r.get("State") != "running" or r.get("Health") not in ("", "healthy")]
            if rows and not bad:
                return
            if time.monotonic() > deadline or any(r.get("State") in ("exited", "dead") for r in bad):
                raise Failure("not running: " + ", ".join(f"{r.get('Service')} {r.get('State')}/{r.get('Health')}"
                                                          for r in bad or rows),
                              f"docker compose -f {ctx.home}/compose.yaml logs --tail 50")
            time.sleep(1)



def replace_public(path: Path, text: str) -> None:
    """Write a world-readable file whole: a temporary beside it, then rename."""
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(text)
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


IMAGE_ENV = "boring-image.env"


def compose_command(home: Path, *args: str) -> list[str]:
    """Compose interpolation: retain the operator's values, then our image."""
    command = ["docker", "compose", "-f", str(home / "compose.yaml")]
    if (home / IMAGE_ENV).exists():
        for path in (home / ".env", home / IMAGE_ENV):
            if path.exists():
                command += ["--env-file", str(path)]
    return [*command, *args]


def migrate_image_env(path: Path) -> None:
    """Remove only the line older deploys owned; keep bytes, mode and owner."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise Failure(f"{path} is not a regular operator env file")
        original = source.read()
    updated = b"".join(line for line in original.splitlines(keepends=True)
                       if not line.startswith(b"BORING_IMAGE="))
    if updated == original:
        return
    fd, temporary = tempfile.mkstemp(prefix=".boring-env-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as target:
            # fchown may clear mode bits; restore the exact mode afterwards.
            current = os.fstat(target.fileno())
            if (current.st_uid, current.st_gid) != (info.st_uid, info.st_gid):
                os.fchown(target.fileno(), info.st_uid, info.st_gid)
            os.fchmod(target.fileno(), stat.S_IMODE(info.st_mode))
            target.write(updated)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)



def rollback_hint(runtime: Any, home: Path, live: str) -> str | None:
    """The way back to the release kept as previous, named by its own VERSION."""
    previous = home / runtime.PREVIOUS
    marker = previous / "VERSION"
    if not marker.is_file() or marker.read_text().strip() == live:
        return None
    return f"back to {marker.read_text().strip()}: sudo {previous}/deploy {home}"


RUNTIMES: dict[str, Any] = {"venv": Venv, "docker": Docker, "files": Files}


# -- steps -------------------------------------------------------------------

def verify(release: Release) -> None:
    verify_files(release.directory)


def verify_copy(copy: Path) -> None:
    """The copy is what runs from HOME later, so it is the copy that must match.
    A copy that does not is removed; nothing else has seen it yet."""
    try:
        verify_files(copy)
    except Failure:
        shutil.rmtree(copy, ignore_errors=True)
        raise


def verify_files(directory: Path) -> None:
    """Every file against MANIFEST.sha256. This catches damage (a truncated
    unpack, an edited file), not tampering: whoever can write the directory can
    rewrite the manifest too. See the README on who may write a release."""
    listed: dict[str, str] = {}
    for line in (directory / "MANIFEST.sha256").read_text().splitlines():
        digest, name = line.split("  ", 1)
        listed[name] = digest
    present = set()
    for path in directory.rglob("*"):
        relative = path.relative_to(directory)
        if relative.parts[0] in GENERATED or relative.as_posix() == "MANIFEST.sha256":
            continue
        if path.is_dir() and not path.is_symlink():
            continue
        present.add(relative.as_posix())
    if present != set(listed):
        extra, missing = sorted(present - set(listed)), sorted(set(listed) - present)
        raise Failure(f"release files differ from MANIFEST.sha256 (extra {extra[:5]}, missing {missing[:5]})")
    for name, digest in listed.items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != digest:
            raise Failure(f"{name} does not match MANIFEST.sha256")


def check_home_path(home: Path, *, missing_ok: bool = False) -> None:
    """Check only HOME and its ancestry. Components own the data inside it."""
    if not home.is_absolute() or home == Path("/") or ".." in home.parts:
        raise Failure(f"{home}: choose an absolute component HOME below /")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    owner = os.geteuid()
    current = Path("/")
    with contextlib.ExitStack() as opened:
        try:
            fd = os.open("/", flags)
            opened.callback(os.close, fd)
            _input_info(fd, current, owner, ancestor=True)
            for part in home.parts[1:]:
                parent = fd
                current = current / part
                try:
                    fd = os.open(part, flags, dir_fd=parent)
                except FileNotFoundError:
                    if missing_ok:
                        # An installer may create this entry. A writable parent
                        # could let another account claim the name beforehand.
                        _input_info(parent, current.parent, owner)
                        return
                    raise Failure(f"{home} does not exist", "install the component first") from None
                opened.callback(os.close, fd)
                _input_info(fd, current, owner, ancestor=current != home)
        except OSError as error:
            raise Failure(f"unsafe component HOME {current}: {error.strerror}") from error


def check_home(release: Release, home: Path) -> None:
    check_home_path(home)
    marker = home / "COMPONENT"
    if os.path.lexists(marker):
        with root_input(marker, owner=os.geteuid()) as source:
            component = source.read_text().strip()
        if component != release.component:
            raise Failure(f"{home} belongs to {component}, not {release.component}")


def check_requires(release: Release) -> None:
    # Shared by --check and deploy, before an install's step zero can mutate the host.
    # Old artifacts omit the requirement and retain their original behavior.
    if release.architecture is not None:
        host = platform.machine().lower()
        aliases = {"amd64": "x86_64", "arm64": "aarch64"}
        if aliases.get(host, host) != aliases.get(release.architecture.lower(), release.architecture.lower()):
            raise Failure(f"artifact architecture is {release.architecture}; host architecture is {host or 'unknown'}")
    if release.glibc_min is not None:
        try:
            libc = os.confstr("CS_GNU_LIBC_VERSION")
        except (OSError, ValueError):
            libc = None
        match = re.fullmatch(r"glibc ([0-9]+)\.([0-9]+)", libc or "")
        if match is None:
            raise Failure(f"needs glibc {release.glibc_min} or newer; host glibc version is unknown")
        actual = tuple(int(part) for part in match.groups())
        wanted = tuple(int(part) for part in release.glibc_min.split("."))
        if actual < wanted:
            raise Failure(f"needs glibc {release.glibc_min} or newer; host has glibc {'.'.join(match.groups())}")
    for component, spec in release.requires.items():
        check_dependency(component, spec)


def check_dependency(component: str, spec: str, *, optional: bool = False) -> None:
    """Check one installed component, read-only; optional allows an absent peer.

    A recorded HOME with no readable version remains an error, not an absent
    optional install. The caller owns when a peer must be compatible.
    """
    known = homes()
    home = Path(known.get(component, f"/opt/{component}"))
    version = installed_version(home, "venv") or installed_version(home, "docker")
    if version is None:
        if optional and component not in known and not any(
                os.path.lexists(home / name) for name in ("current", "release")):
            return
        raise Failure(f"needs {component} {spec}; none found in {home}",
                      "" if component in known else f"record its HOME in {HOMES}")
    if not satisfies(version, spec):
        raise Failure(f"needs {component} {spec}; {home} has {version}", f"deploy {component} first")


def data_changes(release: Release, current: int | None) -> bool:
    """Whether the data generation changes; refuses a change this release cannot make."""
    new = release.data_generation
    if new is None or current is None or current == new:
        return False
    if current > new:
        raise Failure(f"the data is generation {current}; this release reads up to {new}",
                      "going back across a data change means restoring the backup taken before it")
    if release.upgrades_from is not None and current < release.upgrades_from:
        raise Failure(f"the data is generation {current}; this release upgrades from {release.upgrades_from}",
                      "deploy an intermediate release first")
    return True


def decide_backup(release: Release, current: int | None, flag: bool | None) -> bool:
    if not data_changes(release, current):
        return False
    new = release.data_generation
    if flag is not None:
        return flag
    if not sys.stdin.isatty():
        raise Failure(f"the data changes from generation {current} to {new}: pass --backup or --no-backup")
    answer = input(f"The data changes from generation {current} to {new}. Back it up first? [Y/n] ")
    return answer.strip().lower() not in ("n", "no")


def record(ctx: Context, action: str) -> None:
    user = os.environ.get("SUDO_USER") or pwd.getpwuid(os.getuid()).pw_name
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    with open(ctx.home / "DEPLOYED", "a") as log:
        log.write(f"{stamp} {action} {ctx.release.component} {ctx.release.version} "
                  f"(was {ctx.installed or 'none'}) by {user}\n")
    owner = ctx.home / "COMPONENT"
    if not owner.exists():  # claims a fresh HOME, so another component's deploy is refused
        owner.write_text(ctx.release.component + "\n")


def _installer() -> Any:
    try:
        from . import install  # in the package
    except ImportError:  # in a release: deploy.d/boring_install.py
        import boring_install as install  # type: ignore[no-redef]
    return install


def main(directory: str | Path, steps: Any, argv: list[str] | None = None, install: Any = None) -> int:
    """`install`: the component's install steps, if it has them (see install.py);
    then step 0 installs it when nothing is installed yet."""
    directory = Path(directory)
    release = Release.load(directory)
    parser = argparse.ArgumentParser(prog=f"{directory}/deploy",
                                     description=f"Deploy {release.component} {release.version}.")
    parser.add_argument("home", nargs="?", help=f"default: {release.default_home}, or {HOMES}")
    parser.add_argument("--check", action="store_true",
                        help="read-only: verify the unpacked release and the host (steps 0 and 1), write nothing; "
                             "preparing and the component's check happen in the deploy itself")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--backup", dest="backup", action="store_true", default=None)
    group.add_argument("--no-backup", dest="backup", action="store_false")
    installer = _installer() if install is not None else None
    if installer is not None:
        installer.add_options(parser, install, release)
    args = parser.parse_args(argv)
    os.umask(0o022)  # what deploy writes is read by the components' own accounts
    # Absolute once, so homes.json and every later comparison see one spelling.
    home = Path(os.path.abspath(args.home or homes().get(release.component, release.default_home)))
    runtime = RUNTIMES[release.runtime]()
    ctx = Context(release, home, None, log=lambda m: print("  " + m, flush=True))

    def hook(name: str, *extra: Any, default: Any = None) -> Any:
        return getattr(steps, name)(ctx, *extra) if hasattr(steps, name) else default

    def way_back() -> str | None:
        hint = rollback_hint(runtime, home, release.version)
        if hint is None:
            return None
        own = hook("way_back", home / runtime.PREVIOUS)
        return hint if own is None else own

    step, switched = "lock", False
    try:
        if os.geteuid() != 0 and not os.environ.get("BORING_DEPLOY_TEST"):
            raise Failure("run as root (sudo)")
        check_home_path(home, missing_ok=True)
        with contextlib.ExitStack() as held:
            # Before any state a decision depends on is read; held to the record.
            held.enter_context(operation_lock(home, shared=args.check))
            if not args.check and hasattr(steps, "lock"):
                held.enter_context(steps.lock(ctx))
            ctx.installed = installed_version(home, release.runtime)
            step = "verify"
            # Verified before anything else, including an install's step zero.
            verify(release)
            check_requires(release)
            problems = hook("preflight", default=[])
            if not isinstance(problems, list) or any(not isinstance(line, str) for line in problems):
                raise Failure("preflight must return a list of problem strings")
            if problems:
                for line in problems:
                    ctx.log(line)
                raise Failure(f"{len(problems)} preflight problem{'s' if len(problems) != 1 else ''}; nothing is changed",
                              "resolve the listed problems as the release\'s CHANGELOG Upgrade notes say, then run deploy --check again")
            if installer is not None:
                step = "install"
                if not installer.step_zero(ctx, install, args):
                    return 0
                ctx.installed = installed_version(home, release.runtime)
                step = "verify"
            print(f"1 verify  {release.component} {release.version} into {home} "
                  f"(installed: {ctx.installed or 'none'})", flush=True)
            check_home(release, home)
            runtime.check_host(ctx)
            generation = hook("data_generation")
            if args.check:
                if data_changes(release, generation):
                    ctx.log(f"the data changes from generation {generation} to {release.data_generation}: "
                            "the deploy backs it up first with --backup (or asks)")
                print("checked: the release and the host; nothing written "
                      "(preparing and the component's check run in the deploy itself)")
                return 0
            take_backup = decide_backup(release, generation, args.backup)
            step = "prepare"
            print("2 prepare", flush=True)
            runtime.prepare(ctx)
            step = "check"
            print("3 check", flush=True)
            hook("check")
            ctx.running_healthy = hook("healthy")
            if take_backup:
                step = "backup"
                target = home / "backups" / datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
                target.mkdir(parents=True, mode=0o700)
                print(f"- backup  {hook('backup', target)}", flush=True)
            step = "switch"
            print("4 switch", flush=True)
            units = hook("units", default=[])
            if hasattr(runtime, "check_units"):
                runtime.check_units(ctx, units)  # before anything that runs changes
            switched = True
            runtime.switch(ctx, units)
            step = "start"
            stopped = ctx.not_started
            if stopped:
                print(f"5 start  site is stopped: not started (stopped before the update, so left stopped: "
                      f"{', '.join(ctx.stopped)})", flush=True)
                print("6 smoke  skipped: nothing of it runs", flush=True)
            else:
                print("5 start", flush=True)
                if ctx.stopped:
                    ctx.log("stopped before the update, so not started: " + ", ".join(ctx.stopped))
                runtime.wait(ctx, ctx.restarted)
                step = "smoke"
                print("6 smoke", flush=True)
                hook("smoke")
            step = "record"
            older = ctx.installed and version_key(release.version) < version_key(ctx.installed)
            record(ctx, "rollback" if older else "deploy")
            start_hint = ("start it with " + shlex.join(compose_command(home, "up", "-d"))
                          if release.runtime == "docker" else runtime.START)
            completion = hook("completion")
            print(f"7 record  {release.component} {release.version} "
                  + (completion if completion is not None else
                     f"is installed, not started ({start_hint})" if stopped else "is live"), flush=True)
            hint = way_back()
            if hint:
                print(f"  {hint}")
            return 0
    except (Failure, OSError, subprocess.SubprocessError, KeyError, ValueError) as problem:
        print(f"FAILED at {step}: {problem}", file=sys.stderr)
        if isinstance(problem, Failure) and problem.hint:
            print(f"  {problem.hint}", file=sys.stderr)
        if ctx.installing and not switched:
            print("  the first install changed the host but no release runs yet: run the deploy again"
                  " (it reuses what is there)", file=sys.stderr)
        elif not switched and ctx.changed:
            print("  the installed release is untouched, but the host changed before the switch:",
                  file=sys.stderr)
            for line in ctx.changed:
                print(f"    {line}", file=sys.stderr)
        elif not switched and ctx.new is None:
            print("  nothing changed: the installed release is untouched", file=sys.stderr)
        if not switched and ctx.new is not None:
            print("  nothing that runs changed; the prepared release stays for the next attempt", file=sys.stderr)
        elif switched:
            try:
                hint = way_back()
            except (Failure, OSError, subprocess.SubprocessError, KeyError, ValueError) as error:
                print(f"  way back unavailable: {error}", file=sys.stderr)
            else:
                if hint:
                    print(f"  {hint}", file=sys.stderr)
        return 1
