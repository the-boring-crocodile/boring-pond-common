#!/usr/bin/env python3
"""Keep the licence texts of a python-build-standalone build in boring-deploy.

    python scripts/python_notices.py 3.14.8 20261001

The release artifacts ship the install_only CPython that uv installs. Those
builds leave out python/licenses/, the licence texts of the libraries linked
into CPython (OpenSSL, SQLite, libffi, ...). This downloads the matching full
build once from the python-build-standalone release, verifies it against the
release's SHA256SUMS, and keeps its python/licenses/ under
packages/boring-deploy/src/boring_deploy/python_notices/VERSION+TAG/ with a
SOURCE.txt. Needs network and `tar` with zstd. Writes nothing on failure.
"""
import hashlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "packages/boring-deploy/src/boring_deploy/python_notices"
RELEASES = "https://github.com/astral-sh/python-build-standalone/releases/download"
TRIPLE = "x86_64-unknown-linux-gnu"


def fetch(url: str, dest: Path) -> None:
    with urllib.request.urlopen(url, timeout=120) as response, open(dest, "wb") as out:
        shutil.copyfileobj(response, out)


def main(version: str, tag: str) -> int:
    name = f"cpython-{version}+{tag}-{TRIPLE}-pgo+lto-full.tar.zst"
    final = TARGET / f"{version}+{tag}"
    if final.exists():
        print(f"{final} exists")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        fetch(f"{RELEASES}/{tag}/SHA256SUMS", work / "SHA256SUMS")
        expected = {line.split()[1]: line.split()[0]
                    for line in (work / "SHA256SUMS").read_text().splitlines() if line.strip()}
        if name not in expected:
            raise SystemExit(f"{name} is not in release {tag}")
        archive = work / name
        fetch(f"{RELEASES}/{tag}/{name}", archive)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != expected[name]:
            raise SystemExit(f"{name}: sha256 {digest}, release says {expected[name]}")
        subprocess.run(["tar", "--zstd", "-xf", str(archive), "-C", str(work), "python/licenses"], check=True)
        licences = work / "python" / "licenses"
        (licences / "SOURCE.txt").write_text(
            f"python/licenses/ of {name}\nfrom {RELEASES}/{tag}/{name}\nsha256 {digest}\n")
        TARGET.mkdir(parents=True, exist_ok=True)
        shutil.copytree(licences, final)
    print(f"kept {len(list(final.iterdir()))} files in {final}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    sys.exit(main(sys.argv[1], sys.argv[2]))
