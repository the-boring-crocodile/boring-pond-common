"""Assemble a release artifact: the directory a deploy runs from, and its tarball.

    assemble(out, release, steps, files, install=None)

`release` is RELEASE.json's content (component, version, runtime,
default_home, ...); `steps` the component's steps module file; `files` maps
paths inside the release to source files; `install` the component's install
steps (see install.py), if it has them. Writes out/<component>-<version>/
with VERSION, RELEASE.json, deploy, deploy.d/{boring_deploy,steps}.py (with
install steps also uninstall and deploy.d/{boring_install,install_steps}.py),
the files and MANIFEST.sha256, then out/<component>-<version>-linux-x86_64.tar.gz
(sorted, fixed mtime and owner: the same inputs give the same bytes).
`files` must include LICENSE and COPYING.md: a release carries the
component's licence.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

ENGINE = Path(__file__).with_name("__init__.py")
INSTALLER = Path(__file__).with_name("install.py")
LICENCE_FILES = ("LICENSE", "COPYING.md")
# Licence texts of what python-build-standalone links into CPython, per
# VERSION+BUILDTAG; uv installs install_only builds, which leave them out.
PYTHON_NOTICES = Path(__file__).with_name("python_notices")
_START = '''import os
import sys

# The engine and the steps need 3.11 (tomllib, among others): refused here, before they are imported.
if sys.version_info < (3, 11):
    sys.exit("python3 3.11 or newer is needed to run this; this is python3 %d.%d" % tuple(sys.version_info[:2]))
# Isolated (-I) and without bytecode (-B): the release stays as its manifest lists it.
if not (sys.flags.isolated and sys.flags.dont_write_bytecode):
    os.execv(sys.executable, [sys.executable, "-I", "-B", os.path.abspath(__file__), *sys.argv[1:]])
here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, "deploy.d"))
'''
LAUNCHER = '''#!/usr/bin/env python3
"""Deploy this release: sudo ./deploy [HOME] [--check] [--backup | --no-backup] (--help)."""
''' + _START + '''import boring_deploy  # noqa: E402
import steps  # noqa: E402

sys.exit(boring_deploy.main(here, steps))
'''
INSTALLING_LAUNCHER = '''#!/usr/bin/env python3
"""Deploy this release, installing it first where nothing is installed yet:
sudo ./deploy [HOME] [--check] [--install] [--backup | --no-backup] (--help)."""
''' + _START + '''import boring_deploy  # noqa: E402
import install_steps  # noqa: E402
import steps  # noqa: E402

sys.exit(boring_deploy.main(here, steps, install=install_steps))
'''
UNINSTALL = '''#!/usr/bin/env python3
"""Uninstall this component: sudo ./uninstall [HOME] [--delete-data] [--yes] (--help).
State and configuration are kept and listed, never deleted."""
''' + _START + '''import boring_install  # noqa: E402
import install_steps  # noqa: E402

sys.exit(boring_install.uninstall_main(here, install_steps))
'''
EPOCH = 1_700_000_000


def assemble(out: Path, release: dict[str, object], steps: Path, files: dict[str, Path],
             install: Path | None = None) -> Path:
    missing = [n for n in LICENCE_FILES if n not in files]
    if missing:
        raise SystemExit(f"a release carries the component's licence: files lacks {', '.join(missing)}")
    name = f"{release['component']}-{release['version']}"
    root = out / name
    if root.exists():
        raise SystemExit(f"{root} exists; choose a new output directory")
    (root / "deploy.d").mkdir(parents=True)
    (root / "VERSION").write_text(f"{release['version']}\n")
    (root / "RELEASE.json").write_text(json.dumps(release, indent=2, sort_keys=True) + "\n")
    (root / "deploy").write_text(LAUNCHER if install is None else INSTALLING_LAUNCHER)
    os.chmod(root / "deploy", 0o755)
    shutil.copyfile(ENGINE, root / "deploy.d" / "boring_deploy.py")
    shutil.copyfile(steps, root / "deploy.d" / "steps.py")
    if install is not None:
        (root / "uninstall").write_text(UNINSTALL)
        os.chmod(root / "uninstall", 0o755)
        shutil.copyfile(INSTALLER, root / "deploy.d" / "boring_install.py")
        shutil.copyfile(install, root / "deploy.d" / "install_steps.py")
    for target, source in files.items():
        (root / target).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, root / target)
    listed = sorted(p for p in root.rglob("*") if p.is_file())
    (root / "MANIFEST.sha256").write_text("".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(root).as_posix()}\n" for p in listed))
    archive = out / f"{name}-linux-x86_64.tar.gz"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted([root, *root.rglob("*")]):
            info = tar.gettarinfo(str(path), arcname=path.relative_to(out).as_posix())
            info.mtime, info.uid, info.gid, info.uname, info.gname = EPOCH, 0, 0, "root", "root"
            info.mode = 0o755 if path.is_dir() or os.access(path, os.X_OK) else 0o644
            if path.is_file():
                with open(path, "rb") as handle:
                    tar.addfile(info, handle)
            else:
                tar.addfile(info)
    with open(archive, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=EPOCH, filename="") as packed:
        packed.write(buffer.getvalue())
    return archive


def standalone_python(uv: str, version: str, dest: Path) -> Path:
    """dest (a .tar.gz) with python-build-standalone CPython `version` (as
    uv manages it) under the top directory `python/`. Relocatable: the deploy
    unpacks it as the release's app/ and installs the wheels into it.
    python/licenses/ carries the licence texts of the libraries linked into
    it; a build without them in PYTHON_NOTICES stops."""
    subprocess.run([uv, "python", "install", "--quiet", version], check=True)
    found = subprocess.run([uv, "python", "find", "--managed-python", version],
                           capture_output=True, text=True, check=True).stdout.strip()
    top = Path(found).resolve().parent.parent
    notices = python_notices(top, version)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted([top, *top.rglob("*")]):
            if "__pycache__" in path.parts or path.name == "EXTERNALLY-MANAGED":
                continue  # uv marks its Pythons as managed; the release's Python is the release's own
            info = tar.gettarinfo(str(path), arcname=(Path("python") / path.relative_to(top)).as_posix())
            info.mtime, info.uid, info.gid, info.uname, info.gname = EPOCH, 0, 0, "root", "root"
            if not path.is_symlink():  # readable by every service account, whatever the builder's umask
                info.mode = 0o755 if path.is_dir() or os.access(path, os.X_OK) else 0o644
            if path.is_file() and not path.is_symlink():
                with open(path, "rb") as handle:
                    tar.addfile(info, handle)
            else:
                tar.addfile(info)
        for path in [notices, *sorted(notices.iterdir())]:
            info = tar.gettarinfo(str(path), arcname=(
                "python/licenses" if path == notices else f"python/licenses/{path.name}"))
            info.mtime, info.uid, info.gid, info.uname, info.gname = EPOCH, 0, 0, "root", "root"
            info.mode = 0o755 if path.is_dir() else 0o644
            if path.is_dir():
                tar.addfile(info)
            else:
                with open(path, "rb") as handle:
                    tar.addfile(info, handle)
    with open(dest, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=EPOCH, filename="") as packed:
        packed.write(buffer.getvalue())
    return dest


def python_notices(top: Path, version: str) -> Path:
    """The kept licence texts for the python-build-standalone build at `top`
    (its BUILD file names the release tag). Stops when they are missing:
    scripts/python_notices.py in boring-pond-common fetches them."""
    build = top / "BUILD"
    tag = build.read_text().strip() if build.is_file() else ""
    found = PYTHON_NOTICES / f"{version}+{tag}"
    if not tag or not any(found.glob("LICENSE.*")):
        raise SystemExit(f"no licence notices for python-build-standalone CPython {version}+{tag or '?'}: "
                         f"run scripts/python_notices.py {version} {tag or 'TAG'} in boring-pond-common")
    return found
