"""First install and uninstall, for a component whose release carries install steps.

The deploy asks this module first (step 0): is the component installed here?
Reality answers, never a record. The component's install steps
(deploy.d/install_steps.py in a release) supply

    SETTINGS                       the site's choices, given once, at the first install
                                   (a list of Setting, or a function of the Release)
    INSTANCE                       optional: a Setting naming one of several
                                   installations of the component on a host (a
                                   Pirate). Always accepted; ctx.instance holds it.
                                   look then answers for that instance (for none
                                   given: whether any is installed); a first install
                                   of an instance needs it named
    look(ctx) -> Look              what is on the host
    ready(ctx, settings)           optional: checks that need the settings (a config
                                   file, a network); raises before anything changes
    plan(ctx, settings) -> [str]   what install would do
    install(ctx, settings)         does it, reusing what is there (a rerun after it
                                   stopped part-way); the file that makes look say
                                   "installed" comes last
    uninstall(ctx, delete_data) -> [str]   removes what install made and returns
                                   what it keeps (state, config): no longer live,
                                   for a person to delete after checking. It
                                   stops first and refuses to go on unless the
                                   component is stopped; with delete_data it
                                   seals what it keeps of the account (seal)
                                   before the account goes. The change that makes
                                   look say "not installed" comes last, so a
                                   rerun after a failure finishes the job

Three answers from look: installed (deploy as usual), not installed (install,
then the deploy's own steps), irregular (stop; nothing changes). A first install
asks before it acts at a terminal and needs --install without one. Settings
are refused once installed: the site's files are the operator's from then on.

Standard library only, like the engine; a release carries this file as
deploy.d/boring_install.py.
"""
from __future__ import annotations

import argparse
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:  # in the package
    from . import Context, Failure, Release, homes, homes_update, installed_version, operation_lock, record
except ImportError:  # in a release: deploy.d/boring_deploy.py
    from boring_deploy import (Context, Failure, Release, homes, homes_update,  # type: ignore[no-redef]
                               installed_version, operation_lock, record)
_engine = __package__ or "boring_deploy"  # the engine module, by name


@dataclass
class Setting:
    """One site choice. `pattern` checks a text value; a flag is on or off."""
    name: str                 # option name: --<name>
    help: str
    default: str | None = None
    flag: bool = False
    pattern: str | None = None
    directory: bool = False   # an existing directory, made absolute
    file: bool = False        # an existing file, made absolute

    @property
    def key(self) -> str:
        return self.name.replace("-", "_")

    def check(self, value: str) -> str:
        if self.directory:
            path = Path(value).expanduser().resolve()
            if not path.is_dir():
                raise Failure(f"--{self.name}: {value} is not a directory")
            return str(path)
        if self.file:
            path = Path(value).expanduser().resolve()
            if not path.is_file():
                raise Failure(f"--{self.name}: {value} is not a file")
            return str(path)
        if self.pattern and not re.fullmatch(self.pattern, value):
            raise Failure(f"--{self.name}: {value!r} is not {self.help}")
        return value


@dataclass
class Look:
    installed: bool
    irregular: list[str] = field(default_factory=list)  # stop: nothing changes
    notes: list[str] = field(default_factory=list)      # understood: reported, nothing to do


def settings_of(install_steps: Any, release: Release) -> list[Setting]:
    settings = install_steps.SETTINGS
    return list(settings(release) if callable(settings) else settings)


def _instance_option(parser: argparse.ArgumentParser, install_steps: Any) -> None:
    instance = getattr(install_steps, "INSTANCE", None)
    if instance is not None:
        parser.add_argument(f"--{instance.name}", dest="instance", metavar="NAME", help=instance.help)


def _instance(ctx: Context, install_steps: Any, args: argparse.Namespace) -> None:
    instance = getattr(install_steps, "INSTANCE", None)
    value = getattr(args, "instance", None)
    ctx.instance = instance.check(value) if instance is not None and value is not None else None


def add_options(parser: argparse.ArgumentParser, install_steps: Any, release: Release) -> None:
    _instance_option(parser, install_steps)
    group = parser.add_argument_group("first install (only when nothing is installed yet)")
    group.add_argument("--install", action="store_true",
                       help="install without asking (needed without a terminal)")
    for setting in settings_of(install_steps, release):
        if setting.flag:
            group.add_argument(f"--{setting.name}", dest=setting.key, action="store_true", default=None,
                               help=setting.help)
        else:
            extra = f" (default {setting.default})" if setting.default is not None else ""
            group.add_argument(f"--{setting.name}", dest=setting.key, metavar="VALUE", help=setting.help + extra)


def _given(args: argparse.Namespace, settings: list[Setting]) -> dict[str, Any]:
    return {s.key: getattr(args, s.key) for s in settings if getattr(args, s.key, None) is not None}


def _terminal() -> bool:
    return sys.stdin.isatty()


def _settings(given: dict[str, Any], settings: list[Setting]) -> dict[str, Any]:
    """Every setting checked; missing ones asked at a terminal, else named."""
    values, missing = {}, []
    for setting in settings:
        value = given.get(setting.key)
        if setting.flag:
            values[setting.key] = bool(value)
            continue
        if value is None and setting.default is not None:
            value = setting.default
        if value is None and _terminal():
            value = input(f"  {setting.help} (--{setting.name}): ").strip() or None
        if value is None:
            missing.append(f"--{setting.name} ({setting.help})")
            continue
        values[setting.key] = setting.check(value)
    if missing:
        raise Failure("a first install needs " + ", ".join(missing))
    return values


def _recorded_elsewhere(ctx: Context) -> Path | None:
    """homes.json names another HOME for this component, and it exists:
    probably a typo here, not a second installation."""
    recorded = homes().get(ctx.release.component)
    if recorded and Path(recorded).resolve() != ctx.home.resolve() and Path(recorded).exists():
        return Path(recorded)
    return None


def _record_home(ctx: Context) -> None:
    """Claim homes.json's line for this HOME. Step zero's check ran before the
    install, under this HOME's operation lock only: a first install into
    another HOME (another lock) may have recorded itself meanwhile, so the
    check is repeated under homes.lock and never overwrites that line."""
    with homes_update() as known:
        recorded = known.get(ctx.release.component)
        if recorded == str(ctx.home):
            return
        if recorded and Path(recorded).resolve() != ctx.home.resolve() and Path(recorded).exists():
            raise Failure(f"{ctx.release.component} was recorded at {recorded} ({_homes_file()}) while this "
                          f"install ran: {ctx.home} is installed but not recorded",
                          f"keep one: uninstall the other, or this one (sudo ./uninstall {ctx.home})")
        known[ctx.release.component] = str(ctx.home)
    ctx.log(f"recorded {ctx.release.component} -> {ctx.home} in {_homes_file()}")


def _forget_home(ctx: Context, install_steps: Any) -> None:
    """After an uninstall: once nothing of the component is installed here any
    more (for instances: the last one is gone and its code too), its line in
    homes.json goes, so a later install may choose another HOME."""
    if homes().get(ctx.release.component) != str(ctx.home):
        return
    instance, ctx.instance = ctx.instance, None
    try:
        if install_steps.look(ctx).installed:
            return
    finally:
        ctx.instance = instance
    with homes_update() as known:
        if known.get(ctx.release.component) != str(ctx.home):
            return
        del known[ctx.release.component]
    ctx.log(f"removed {ctx.release.component} from {_homes_file()}")


# -- sealing what an account leaves behind -------------------------------------

SEAL_OWNER = (0, 0)  # root:root; tests that cannot chown say so


def _mount_points() -> set[str]:
    with open("/proc/self/mountinfo", encoding="utf-8") as entries:
        return {re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), line.split(" ", 5)[4])
                for line in entries}


def seal(ctx: Context, folders: Iterable[Path], uid: int) -> None:
    """Before userdel frees `uid`: each kept top-level folder the account owns
    becomes root:root 0700, so a later account given the same number cannot
    reach what is inside (which keeps its owner). Call it right before userdel
    and do not delete the account when it raises.

    All folders are checked first: a symlink, something not a folder, a mount
    point or a folder of another account stops it before any change. Then each
    is opened without following a symlink, checked to be the one looked at, and
    changed through that descriptor: mode first, then owner (a stop between
    leaves it the account's, and a rerun seals it). A folder that is gone, or
    root's already (sealed by an earlier run, or never the account's), is left
    as it is."""
    mounts, todo = _mount_points(), []
    for folder in folders:
        try:
            info = folder.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise Failure(f"{folder} is a symlink: not sealed, the account is kept",
                          "check what it names, replace the link by the folder, then run uninstall again")
        if not stat.S_ISDIR(info.st_mode):
            raise Failure(f"{folder} is not a folder: not sealed, the account is kept")
        if str(folder) in mounts:
            raise Failure(f"{folder} is a mount point: not sealed, the account is kept",
                          "unmount it (or seal it by hand: chown root:root, chmod 0700), then run uninstall again")
        if info.st_uid == SEAL_OWNER[0]:
            continue
        if info.st_uid != uid:
            raise Failure(f"{folder} belongs to uid {info.st_uid}, not the account's ({uid}): not sealed, "
                          "the account is kept")
        todo.append((folder, info))
    for folder, info in todo:
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise Failure(f"{folder} changed while it was checked: not sealed, the account is kept")
            os.fchmod(fd, 0o700)
            os.fchown(fd, *SEAL_OWNER)
        finally:
            os.close(fd)
        ctx.log(f"sealed {folder}: root's, 0700 (inside, files keep uid {uid})")


def report(ctx: Context, look: Look) -> None:
    for note in look.notes:
        ctx.log(f"note: {note}")
    if look.irregular:
        raise Failure("irregular, so nothing is changed:" + "".join(f"\n    {i}" for i in look.irregular),
                      "look at each finding; when it is understood, fix it by hand and run again")


def step_zero(ctx: Context, install_steps: Any, args: argparse.Namespace) -> bool:
    """Before step 1. True: go on with the deploy. False: --check of a first
    install, which stops after its plan."""
    print(f"0 install? {ctx.release.component} in {ctx.home}")
    elsewhere = _recorded_elsewhere(ctx)
    if elsewhere is not None:
        raise Failure(f"{ctx.release.component} is recorded at {elsewhere} ({_homes_file()}), which exists",
                      f"deploy there, or uninstall it first")
    owner = ctx.home / "COMPONENT"
    if owner.is_file() and owner.read_text().strip() != ctx.release.component:
        raise Failure(f"{ctx.home} belongs to {owner.read_text().strip()}, not {ctx.release.component}")
    _instance(ctx, install_steps, args)
    look = install_steps.look(ctx)
    report(ctx, look)
    settings_list = settings_of(install_steps, ctx.release)
    given = _given(args, settings_list)
    if look.installed:
        if given:
            raise Failure("installed already; settings are for a first install only: "
                          + ", ".join(f"--{k.replace('_', '-')}" for k in given),
                          "the site's files are yours now; change them there")
        ctx.log("installed" + (f": {ctx.instance}" if ctx.instance else ""))
        return True
    instance = getattr(install_steps, "INSTANCE", None)
    if instance is not None and ctx.instance is None:
        raise Failure(f"nothing installed here yet: a first install needs --{instance.name} ({instance.help})")
    settings = _settings(given, settings_list)
    if hasattr(install_steps, "ready"):
        install_steps.ready(ctx, settings)
    ctx.log("not installed; a first install would:")
    for line in install_steps.plan(ctx, settings):
        ctx.log(f"  {line}")
    if args.check:
        print("checked: nothing written (a first install, not done)")
        return False
    if not args.install:
        if not _terminal():
            raise Failure("not installed here", "pass --install to install (no terminal to ask)")
        what = ctx.release.component + (f" {ctx.instance}" if ctx.instance else "")
        answer = input(f"Install {what} into {ctx.home}? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            raise Failure("not installed (declined)")
    ctx.installing = True
    install_steps.install(ctx, settings)
    _record_home(ctx)
    after = install_steps.look(ctx)
    if not after.installed or after.irregular:
        raise Failure("install finished but the host does not look installed:"
                      + "".join(f"\n    {i}" for i in after.irregular))
    ctx.log("installed; the first deploy follows")
    return True


def _homes_file() -> Path:
    return sys.modules[_engine].HOMES  # read when used: tests point it elsewhere


def uninstall_main(directory: str | Path, install_steps: Any, argv: list[str] | None = None) -> int:
    directory = Path(directory)
    release = Release.load(directory)
    parser = argparse.ArgumentParser(prog=f"{directory}/uninstall",
                                     description=f"Uninstall {release.component}. State and configuration "
                                                 "are kept and listed; deleting them is a person's job.")
    parser.add_argument("home", nargs="?", help=f"default: {release.default_home}, or {_homes_file()}")
    _instance_option(parser, install_steps)
    parser.add_argument("--delete-data", action="store_true",
                        help="also remove the account, once what it owned is sealed (root's, 0700); state and configuration are never deleted")
    parser.add_argument("--yes", action="store_true", help="do not ask (needed without a terminal)")
    args = parser.parse_args(argv)
    home = Path(os.path.abspath(args.home or homes().get(release.component, release.default_home)))
    ctx = Context(release, home, None, log=lambda m: print("  " + m, flush=True))
    try:
        if os.geteuid() != 0 and not os.environ.get("BORING_DEPLOY_TEST"):
            raise Failure("run as root (sudo)")
        with operation_lock(home):  # before look; held to the record and homes.json
            return _uninstall(ctx, install_steps, args)
    except (Failure, OSError, subprocess.SubprocessError, KeyError, ValueError) as problem:
        print(f"FAILED: {problem}", file=sys.stderr)
        if isinstance(problem, Failure) and problem.hint:
            print(f"  {problem.hint}", file=sys.stderr)
        return 1


def _uninstall(ctx: Context, install_steps: Any, args: argparse.Namespace) -> int:
    release, home = ctx.release, ctx.home
    ctx.installed = installed_version(home, release.runtime)
    _instance(ctx, install_steps, args)
    instance = getattr(install_steps, "INSTANCE", None)
    if instance is not None and ctx.instance is None:
        raise Failure(f"name the one to uninstall: --{instance.name}")
    what = release.component + (f" {ctx.instance}" if ctx.instance else "")
    print(f"1 look  {what} in {home}")
    look = install_steps.look(ctx)
    report(ctx, look)
    if not look.installed:
        print("not installed here: nothing to do")
        return 0
    if not args.yes:
        if not _terminal():
            raise Failure("asks first", "pass --yes (no terminal to ask)")
        data = " and its account" if args.delete_data else ""
        if input(f"Uninstall {what}{data} from {home}? [y/N] ").strip().lower() not in ("y", "yes"):
            raise Failure("not uninstalled (declined)")
    print("2 uninstall")
    kept = install_steps.uninstall(ctx, args.delete_data)
    record(ctx, "uninstall" + (f":{ctx.instance}" if ctx.instance else ""))
    _forget_home(ctx, install_steps)
    print(f"3 done  {what} is uninstalled")
    if kept:
        print("  kept, no longer live (delete by hand once nothing in them is needed):")
        for path in kept:
            print(f"    {path}")
    return 0
