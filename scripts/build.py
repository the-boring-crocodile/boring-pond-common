#!/usr/bin/env python3
"""Build wheels and sdists for all Common packages into dist/.

Uses uv (from PATH, or $UV) with pinned build requirements
(scripts/build-constraints.txt), builds from a copy of the tracked paths (their
working-tree contents, so uncommitted edits are included: build from a clean
checkout for a release) with the file modes git records (0644 or 0755), and
uses a fixed SOURCE_DATE_EPOCH. The wheels and sdists therefore depend on the
package contents only, not on leftovers, the commit history, the builder's
umask or ownership. Writes SHA256SUMS. The output directory must not exist.

    python scripts/build.py [--out DIR]
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import gzip
import io
import sys
import tarfile
import zipfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("boring-registry", "boring-file-core", "boring-service-kit", "boring-worker-kit", "boring-deploy")


EPOCH = 315532800  # 1980-01-01, the earliest time a zip archive can record


def normalize_sdist(path: Path) -> None:
    """Rewrite an sdist with sorted entries, fixed times and owners and a fixed gzip header."""
    with tarfile.open(path, "r:gz") as source:
        members = [(m, source.extractfile(m).read() if m.isfile() else None)
                   for m in sorted(source.getmembers(), key=lambda m: m.name)]
    with open(path, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped, \
            tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as target:
        for member, data in members:
            member.mtime, member.uid, member.gid, member.uname, member.gname = EPOCH, 0, 0, "", ""
            member.mode = 0o755 if member.isdir() or member.mode & 0o100 else 0o644
            member.pax_headers = {}
            target.addfile(member, io.BytesIO(data) if data is not None else None)


def normalize_wheel(path: Path) -> None:
    """Rewrite a wheel with fixed entry modes (0644, 0755 if executable) and times.

    The wheel builder writes its own files (METADATA, RECORD, ...) under the
    builder's umask; RECORD hashes contents only, so modes can be fixed after."""
    with zipfile.ZipFile(path) as source:
        entries = [(info, source.read(info)) for info in source.infolist()]
    with zipfile.ZipFile(path, "w") as target:
        for info, data in entries:
            fixed = zipfile.ZipInfo(info.filename, date_time=(1980, 1, 1, 0, 0, 0))
            fixed.compress_type = zipfile.ZIP_DEFLATED
            fixed.create_system = 3
            executable = (info.external_attr >> 16) & 0o100
            fixed.external_attr = (0o100755 if executable else 0o100644) << 16
            target.writestr(fixed, data)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        raise SystemExit("uv not found: put it on PATH or set UV=/path/to/uv")
    if subprocess.run([sys.executable, str(ROOT / "scripts/version.py"), "--check"]).returncode:
        raise SystemExit("versions disagree; run scripts/version.py")
    out = args.out.resolve()
    if out.exists():
        raise SystemExit(f"{out} exists; choose a new output directory")
    out.mkdir(parents=True)
    env = dict(os.environ, SOURCE_DATE_EPOCH=str(EPOCH))
    # "<mode> <object> <stage>\t<path>": git's own file mode, not the checkout's.
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-s", "-z", "packages"],
                             capture_output=True, text=True, check=True).stdout.split("\0")
    with tempfile.TemporaryDirectory(prefix="common-build-") as stage:
        for entry in filter(None, tracked):
            info, name = entry.split("\t", 1)
            target = Path(stage) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)  # contents only, no build leftovers
            target.chmod(0o755 if info.split()[0] == "100755" else 0o644)
        for directory in [Path(stage), *Path(stage).rglob("*")]:
            if directory.is_dir():
                directory.chmod(0o755)
        for name in PACKAGES:
            subprocess.run([uv, "build", "--quiet", "--build-constraint",
                            str(ROOT / "scripts/build-constraints.txt"), "--out-dir", str(out),
                            str(Path(stage) / "packages" / name)], env=env, check=True)
    for sdist in out.glob("*.tar.gz"):
        normalize_sdist(sdist)
    for wheel in out.glob("*.whl"):
        normalize_wheel(wheel)
    sums = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}"
            for p in sorted(out.iterdir()) if p.suffix in (".whl", ".gz")]
    (out / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    print("\n".join(sums))
    return 0


if __name__ == "__main__":
    sys.exit(main())
