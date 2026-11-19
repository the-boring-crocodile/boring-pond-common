"""Version CLI updates a complete source checkout without producing artifacts."""
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def checkout(tmp_path):
    files = [ROOT / "VERSION", ROOT / "scripts/version.py",
             ROOT / "packages/boring-service-kit/src/boring_service_kit/__init__.py",
             *(ROOT / "packages").glob("*/pyproject.toml")]
    for source in files:
        target = tmp_path / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return tmp_path


def run(root, *arguments):
    return subprocess.run([sys.executable, "-I", "-B", str(root / "scripts/version.py"), *arguments],
                          cwd=root, capture_output=True, text=True)


@pytest.mark.parametrize("version", ["0.8.0rc1", "0.8.0rc13", "0.8.0.dev1", "0.8.0", "1.0.0rc2"])
def test_set_and_check(checkout, version):
    result = run(checkout, version)
    assert result.returncode == 0, result.stderr
    assert (checkout / "VERSION").read_text().strip() == version
    upper = "2.0" if version.startswith("1.") else "0.9"
    for path in (checkout / "packages").glob("*/pyproject.toml"):
        project = tomllib.loads(path.read_text())["project"]
        assert project["version"] == version
        for dependency in project.get("dependencies", []):
            if dependency.startswith("boring-"):
                assert dependency.split(">=", 1)[1] == f"{version},<{upper}"
    assert f'__version__ = "{version}"' in (
        checkout / "packages/boring-service-kit/src/boring_service_kit/__init__.py").read_text()
    assert run(checkout, "--check").returncode == 0
    changed = checkout / "packages/boring-deploy/pyproject.toml"
    changed.write_text(changed.read_text().replace(f'version = "{version}"', 'version = "0.0.0"'))
    assert run(checkout, "--check").returncode == 1


@pytest.mark.parametrize("version", ["0.8", "0.8.0rc", "0.8.0rc1junk", "0.8.0post1"])
def test_invalid_version_leaves_checkout_unchanged(checkout, version):
    before = {p.relative_to(checkout): p.read_bytes() for p in checkout.rglob("*") if p.is_file()}
    assert run(checkout, version).returncode != 0
    assert {p.relative_to(checkout): p.read_bytes() for p in checkout.rglob("*") if p.is_file()} == before
