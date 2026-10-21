#!/usr/bin/env python3
"""The one check for Common: versions, build, and every package's tests run
against the built wheels in a fresh environment (not against the source tree).

    python scripts/check.py [--python /usr/bin/python3.12]

Needs uv (PATH or $UV) and network access to the package index for third-party
dependencies and pytest.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("boring-registry", "boring-file-core", "boring-service-kit", "boring-worker-kit", "boring-deploy")
sys.path.insert(0, str(ROOT / "packages/boring-deploy/src"))  # the shared steps, from this tree
from boring_deploy.develop import run, uv as find_uv  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    uv = find_uv()
    with tempfile.TemporaryDirectory(prefix="common-check-") as tmp:
        tmp = Path(tmp)
        run(sys.executable, ROOT / "scripts/build.py", "--out", tmp / "dist", env=dict(os.environ, UV=uv))
        venv = tmp / "venv"
        run(uv, "venv", "--quiet", "--python", args.python, venv)
        env = dict(os.environ, VIRTUAL_ENV=str(venv))
        wheels = sorted((tmp / "dist").glob("*.whl"))
        run(uv, "pip", "install", "--quiet", "pytest", *wheels, env=env)
        python = venv / "bin" / "python"
        failed = []
        for name in PACKAGES:
            tests = ROOT / "packages" / name / "tests"
            # Copy tests out of the tree so imports resolve to the installed wheels.
            copy = tmp / "tests" / name
            for source in tests.rglob("*"):
                if source.is_file() and "__pycache__" not in source.parts:
                    target = copy / source.relative_to(tests)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)  # contents only: source ACLs/xattrs do not travel
            print(f"== {name}", flush=True)
            if subprocess.run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider", copy], cwd=tmp).returncode:
                failed.append(name)
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    print("all Common checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
