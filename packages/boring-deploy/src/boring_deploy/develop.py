"""The development steps every Pond component shares: check, lock and build.

A component's scripts/ keep only what is theirs (which packages, which tests,
which artifact) and take the steps from here. They load this module straight
from the Common wheels they are given (a component's scripts/_develop.py
unpacks the boring_deploy wheel to a temporary folder and imports `develop`
and `artifact` from there; artifact copies the engine's own files, so a zip
import is not enough).

Standard library only; uv (PATH or $UV) does the Python work.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# python-build-standalone releases the artifacts ship, pinned per target Python.
PYTHONS = {"3.14": "3.14.8", "3.12": "3.12.15", "3.10": "3.10.22"}
# Linux wheel tags a release may use, newest first.
LINUX_PLATFORMS = ("manylinux_2_39_x86_64", "manylinux2014_x86_64", "linux_x86_64")


def run(*command: object, **kwargs) -> subprocess.CompletedProcess:
    """Run a command, shown first; a failure stops the script."""
    print("+ " + " ".join(str(part) for part in command), flush=True)
    return subprocess.run([str(part) for part in command], check=True, **kwargs)


def uv() -> str:
    found = os.environ.get("UV") or shutil.which("uv")
    if not found:
        raise SystemExit("uv not found: put it on PATH or set UV=/path/to/uv")
    return found


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


def arguments(doc: str, *, build: bool = False) -> argparse.ArgumentParser:
    """The options every script takes; a script adds its own before parse_args()."""
    parser = argparse.ArgumentParser(description=doc.splitlines()[0])
    parser.add_argument("--common-wheels", type=Path, required=True, help="the Common dist (wheels)")
    if build:
        parser.add_argument("--out", type=Path, required=True, help="a new directory")
        parser.add_argument("--python-version", choices=sorted(PYTHONS), default="3.14")
        parser.add_argument("--allow-dirty", action="store_true", help="a throwaway build from a dirty tree")
    else:
        parser.add_argument("--python", default=sys.executable, help="the interpreter for the check")
    return parser


# --- copies of the tracked files: nothing is built inside the tree -----------

def tracked(root: Path, target: Path, sub: str = ".") -> Path:
    """Copy the files git tracks under `sub` (contents only) to target."""
    names = git(root, "ls-files", "-z", "--", sub).split("\0")
    for name in filter(None, names):
        if (root / name).is_file():
            destination = target / Path(name).relative_to(sub)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / name, destination)
    return target


def package(root: Path, target: Path, sub: str = ".") -> Path:
    """Copy one package's tracked src/, pyproject.toml and README.md: what a wheel is built from."""
    names = git(root, "ls-files", "-z", "--", sub).split("\0")
    for name in filter(None, names):
        relative = Path(name).relative_to(sub)
        if relative.parts[0] in ("src", "pyproject.toml", "README.md"):
            (target / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / name, target / relative)
    return target


def plain_copy(source: Path, target: Path) -> Path:
    """Copy a tree without caches and build leftovers."""
    for item in source.rglob("*"):
        if any(part == "__pycache__" or part.endswith(".egg-info") for part in item.parts):
            continue
        if item.is_file():
            destination = target / item.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, destination)
    return target


def clean_tree(root: Path, allow_dirty: bool) -> None:
    if git(root, "status", "--porcelain") and not allow_dirty:
        raise SystemExit("working tree is not clean (or pass --allow-dirty for a throwaway build)")


def new_directory(path: Path) -> Path:
    path = path.resolve()
    if path.exists():
        raise SystemExit(f"{path} exists; choose a new directory")
    path.mkdir(parents=True)
    return path


# --- check -----------------------------------------------------------------------

def environment(uv_path: str, python: str, venv: Path, locks: list[Path], common_wheels: Path,
                packages: list[Path], constraints: Path, tools: list[str] = ()) -> Path:
    """A new venv: the locks with --require-hashes, the packages without dependencies,
    then the test tools (requirement specs, or `-r file`). Returns its interpreter."""
    run(uv_path, "venv", "--quiet", "--python", python, venv)
    interpreter = venv / "bin" / "python"
    requirements = [arg for lock in locks for arg in ("-r", lock)]
    run(uv_path, "pip", "install", "--quiet", "--python", interpreter, "--require-hashes",
        "--find-links", common_wheels.resolve(), *requirements)
    run(uv_path, "pip", "install", "--quiet", "--python", interpreter, "--no-deps",
        "--build-constraint", constraints, *packages)
    if tools:
        run(uv_path, "pip", "install", "--quiet", "--python", interpreter, *tools)
    return interpreter


# --- lock ------------------------------------------------------------------------

def lock_arguments(doc: str) -> argparse.ArgumentParser:
    parser = arguments(doc)
    parser.add_argument("--constraint", type=Path, help="optional extra constraints (first lock only)")
    parser.add_argument("--upgrade-package", action="append", default=[])
    parser.add_argument("--fresh", action="store_true",
                        help="ignore the existing lock (its pins and hashes are otherwise reused)")
    return parser


def lock(args: argparse.Namespace, output: Path, inputs: list[Path], *, find_links: list[Path] = (),
         no_emit: list[str] = (), python_version: str = "3.10", extra: list[str] = (),
         how: str = "python scripts/lock.py --common-wheels <Common dist>") -> None:
    """uv pip compile: one universal lock with hashes; Common from args.common_wheels.
    `how` is the command the lock's header names for regenerating it."""
    command = [uv(), "pip", "compile", "--quiet", "--universal", "--python-version", python_version,
               "--generate-hashes", "--find-links", str(args.common_wheels.resolve()),
               "--custom-compile-command", how,
               "-o", str(output), *map(str, inputs), *extra]
    for link in find_links:
        command += ["--find-links", str(link)]
    for name in no_emit:
        command += ["--no-emit-package", name]
    if args.constraint:
        command += ["--constraint", str(args.constraint)]
    if args.fresh:
        output.unlink(missing_ok=True)
    for name in args.upgrade_package:
        command += ["--upgrade-package", name]
    run(*command)
    print(f"wrote {output.name}")


# --- build -----------------------------------------------------------------------

# pip evaluates lock markers (python_full_version < '3.11', sys_platform == 'win32')
# against the interpreter it runs on, not the --python-version/--platform target.
# So the lock is filtered for the target first, with pip's own marker parser.
TARGET_FILTER = r"""
import sys
from pip._vendor.packaging.markers import Marker
source, dest, version, platform = sys.argv[1:]
env = {"python_version": version, "python_full_version": version + ".0",
       "implementation_name": "cpython", "platform_python_implementation": "CPython",
       "sys_platform": platform, "os_name": {"linux": "posix", "win32": "nt"}[platform],
       "platform_system": {"linux": "Linux", "win32": "Windows"}[platform],
       "platform_machine": {"linux": "x86_64", "win32": "AMD64"}[platform]}
logical, out = "", []
for line in open(source, encoding="utf-8").read().splitlines():
    if line.rstrip().endswith("\\"):
        logical += line.rstrip()[:-1] + " "
        continue
    logical += line
    text, logical = logical.strip(), ""
    if not text or text.startswith("#"):
        continue
    if text.startswith("-"):
        raise SystemExit("unexpected option line in lock: " + text[:60])
    head, *hashes = text.split(" --hash=")
    requirement, _, marker = head.partition(";")
    if marker.strip() and not Marker(marker.strip()).evaluate(env):
        continue
    if not hashes:
        raise SystemExit("unhashed lock entry: " + requirement.strip())
    out.append(" ".join([requirement.strip()] + ["--hash=" + h.strip() for h in hashes]))
open(dest, "w", encoding="utf-8").write("\n".join(out) + "\n")
"""


def target_lock(python: Path, source: Path, dest: Path, version: str, platform: str) -> Path:
    """Write the lock entries that apply to the target, markers resolved."""
    subprocess.run([str(python), "-I", "-c", TARGET_FILTER, str(source), str(dest), version, platform],
                   check=True)
    return dest


def pip_environment(uv_path: str, directory: Path) -> Path:
    """A throwaway venv with pip (for `pip download` to a target and TARGET_FILTER)."""
    subprocess.run([uv_path, "venv", "--quiet", "--seed", str(directory)], check=True)
    return directory / "bin" / "python"


def wheelhouse(root: Path, out: Path, *, sources: list[str], lock_file: Path, common_wheels: Path,
               python_version: str, constraints: Path, platform: str = "linux",
               platforms: tuple[str, ...] = LINUX_PLATFORMS) -> dict[str, str]:
    """Build the component's wheels (from a copy of the tracked files, SOURCE_DATE_EPOCH
    of the last commit) and download every locked wheel for the target into out.
    Returns {wheel name: sha256}."""
    uv_path = uv()
    with tempfile.TemporaryDirectory(prefix="boring-wheelhouse-") as tmp:
        stage = tracked(root, Path(tmp) / "src")
        epoch = git(root, "log", "-1", "--format=%ct").strip()
        for source in sources:
            subprocess.run([uv_path, "build", "--quiet", "--wheel", "--build-constraint", str(constraints),
                            "--out-dir", str(out), str(stage / source)],
                           env=dict(os.environ, SOURCE_DATE_EPOCH=epoch), check=True)
        python = pip_environment(uv_path, Path(tmp) / "pip")
        command = [str(python), "-m", "pip", "download", "--quiet", "--no-deps", "--require-hashes",
                   "--only-binary=:all:", "--python-version", python_version, "--implementation", "cp",
                   "--find-links", str(common_wheels.resolve()), "--dest", str(out),
                   "-r", str(target_lock(python, lock_file, Path(tmp) / "target.lock", python_version, platform))]
        for tag in platforms:
            command += ["--platform", tag]
        subprocess.run(command, check=True)
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.glob("*.whl"))}


def record(root: Path, out: Path, wheels: dict[str, str], lock_file: Path, python_version: str,
           target: str = "linux/x86_64") -> None:
    """SHA256SUMS and MANIFEST.json beside the wheels: what was built from what."""
    (out / "SHA256SUMS").write_text("".join(f"{digest}  {name}\n" for name, digest in wheels.items()))
    (out / "MANIFEST.json").write_text(json.dumps({
        "source_commit": git(root, "rev-parse", "HEAD").strip(), "dirty": bool(git(root, "status", "--porcelain")),
        "python": python_version, "target": target,
        "requirements_lock_sha256": hashlib.sha256(lock_file.read_bytes()).hexdigest(),
        "wheels": wheels}, indent=2) + "\n")
