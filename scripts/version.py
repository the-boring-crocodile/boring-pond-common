#!/usr/bin/env python3
"""Lockstep version for all Common packages.

VERSION at the repository root is the single source. This tool writes it into
each package's pyproject.toml, the internal dependency ranges and
boring_service_kit.__version__, or (with --check) fails if anything disagrees.

    python scripts/version.py --check
    python scripts/version.py 0.4.1      # set, then commit and tag v0.4.1
    python scripts/version.py 0.5.0.dev1 # a development version: commit, no tag
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("boring-registry", "boring-file-core", "boring-service-kit", "boring-worker-kit", "boring-deploy")
# Dependency entries only: indented list items such as `    "boring-registry>=0.4.0,<0.5",`.
INTERNAL = re.compile(r'(?m)^(\s+)"(boring-(?:registry|file-core|service-kit|worker-kit))[^"]*"')
SERVICE_KIT_INIT = ROOT / "packages/boring-service-kit/src/boring_service_kit/__init__.py"


RELEASE = r"\d+\.\d+\.\d+"
VERSION = re.compile(RELEASE + r"(?:\.dev\d+)?")  # 0.7.0, or 0.7.0.dev1 while a release is prepared


def internal_range(version):
    major, minor, _ = (int(x) for x in re.match(RELEASE, version).group().split("."))
    upper = f"{major + 1}.0" if major else f"0.{minor + 1}"
    return f">={version},<{upper}"


def expected(version):
    files = {}
    for name in PACKAGES:
        path = ROOT / "packages" / name / "pyproject.toml"
        text = path.read_text()
        text = re.sub(r'(?m)^version = "[^"]*"$', f'version = "{version}"', text, count=1)
        text = INTERNAL.sub(lambda m: f'{m.group(1)}"{m.group(2)}{internal_range(version)}"', text)
        files[path] = text
    text = SERVICE_KIT_INIT.read_text()
    files[SERVICE_KIT_INIT] = re.sub(r'(?m)^__version__ = "[^"]*"$', f'__version__ = "{version}"', text, count=1)
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", nargs="?")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.version:
        if not VERSION.fullmatch(args.version):
            raise SystemExit("version must be MAJOR.MINOR.PATCH, or MAJOR.MINOR.PATCH.devN")
        (ROOT / "VERSION").write_text(args.version + "\n")
    version = (ROOT / "VERSION").read_text().strip()
    stale = [path for path, text in expected(version).items() if path.read_text() != text]
    if args.check:
        if stale:
            print("not at " + version + ": " + ", ".join(str(p.relative_to(ROOT)) for p in stale))
            return 1
        print("all packages at " + version)
        return 0
    for path, text in expected(version).items():
        path.write_text(text)
    print("set all packages to " + version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
