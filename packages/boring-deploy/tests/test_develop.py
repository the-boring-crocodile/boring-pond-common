"""The shared development steps: copies of tracked files, lock and check commands."""
import argparse
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from boring_deploy import develop


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg/src/thing").mkdir(parents=True)
    (root / "pkg/src/thing/__init__.py").write_text("X = 1\n")
    (root / "pkg/pyproject.toml").write_text("[project]\nname = 'thing'\n")
    (root / "pkg/tests").mkdir()
    (root / "pkg/tests/test_thing.py").write_text("")
    (root / "notes.md").write_text("tracked\n")
    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c"]):
        subprocess.run(["git", "-C", str(root), *args], check=True)
    (root / "untracked.txt").write_text("not copied\n")
    (root / "pkg/src/thing/__pycache__").mkdir()
    (root / "pkg/src/thing/__pycache__/x.pyc").write_text("")
    return root


def files(directory):
    return sorted(str(p.relative_to(directory)) for p in directory.rglob("*") if p.is_file())


def test_tracked_copies_only_what_git_tracks(repo, tmp_path):
    copy = develop.tracked(repo, tmp_path / "copy")
    assert files(copy) == ["notes.md", "pkg/pyproject.toml", "pkg/src/thing/__init__.py", "pkg/tests/test_thing.py"]


def test_package_copies_what_a_wheel_is_built_from(repo, tmp_path):
    copy = develop.package(repo, tmp_path / "copy", "pkg")
    assert files(copy) == ["pyproject.toml", "src/thing/__init__.py"]


@pytest.mark.parametrize("sub", [".", "pkg"])
def test_declared_licences_are_copied_into_the_built_wheel(repo, tmp_path, sub):
    import zipfile
    base = repo / sub
    (base / "src/thing").mkdir(parents=True, exist_ok=True)
    (base / "src/thing/__init__.py").write_text("X = 1\n")
    (base / "notices").mkdir(exist_ok=True)
    (base / "LICENSE").write_text("synthetic licence\n")
    (base / "notices/NOTICE.txt").write_text("synthetic notice\n")
    (base / "pyproject.toml").write_text(
        "[build-system]\nrequires = ['setuptools']\nbuild-backend = 'setuptools.build_meta'\n"
        "[project]\nname = 'thing'\nversion = '1.0'\nlicense = 'MIT'\n"
        "license-files = ['LICENSE', 'notices/*.txt']\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    (base / "notices/private.txt").write_text("untracked, never ship\n")
    copy = develop.package(repo, tmp_path / "copy", sub)
    assert (copy / "LICENSE").read_text() == "synthetic licence\n"
    assert (copy / "notices/NOTICE.txt").read_text() == "synthetic notice\n"
    assert not (copy / "notices/private.txt").exists()
    constraints = Path(__file__).with_name("build-constraints.txt")
    if not constraints.exists():  # direct run in the source checkout
        constraints = Path(__file__).resolve().parents[3] / "scripts/build-constraints.txt"
    built = subprocess.run([develop.uv(), "build", "--wheel", "--build-constraint", str(constraints),
                            "--out-dir", str(tmp_path / "wheels"), str(copy)],
                           capture_output=True, text=True)
    assert built.returncode == 0, built.stdout + built.stderr
    assert "did not match any files" not in built.stdout + built.stderr
    (wheel,) = (tmp_path / "wheels").glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        assert archive.read("thing-1.0.dist-info/licenses/LICENSE") == b"synthetic licence\n"
        assert archive.read("thing-1.0.dist-info/licenses/notices/NOTICE.txt") == b"synthetic notice\n"


@pytest.mark.parametrize("pattern", ["missing.txt", "untracked.txt", "../notes.md"])
def test_missing_untracked_or_outside_licence_refuses_before_copy(repo, tmp_path, pattern):
    (repo / "pkg/pyproject.toml").write_text("[project]\nlicense-files = [" + repr(pattern) + "]\n")
    (repo / "pkg/untracked.txt").write_text("not tracked\n")
    with pytest.raises(ValueError, match="license-files pattern"):
        develop.package(repo, tmp_path / "copy", "pkg")
    assert not (tmp_path / "copy").exists()


def test_plain_copy_leaves_caches_out(repo, tmp_path):
    copy = develop.plain_copy(repo / "pkg", tmp_path / "copy")
    assert "src/thing/__pycache__/x.pyc" not in files(copy)
    assert "src/thing/__init__.py" in files(copy)


def test_a_dirty_tree_refuses_a_build_unless_allowed(repo):
    with pytest.raises(SystemExit, match="not clean"):
        develop.clean_tree(repo, allow_dirty=False)
    develop.clean_tree(repo, allow_dirty=True)


def test_new_directory_never_reuses_one(tmp_path):
    assert develop.new_directory(tmp_path / "out").is_dir()
    with pytest.raises(SystemExit, match="exists"):
        develop.new_directory(tmp_path / "out")


def test_lock_command(tmp_path, monkeypatch):
    monkeypatch.setenv("UV", "/bin/uv")
    args = argparse.Namespace(common_wheels=tmp_path, constraint=None, upgrade_package=["boring-registry"], fresh=False)
    with mock.patch.object(develop, "run") as run:
        develop.lock(args, tmp_path / "requirements.lock", [tmp_path / "pyproject.toml"],
                     find_links=[tmp_path / "built"], no_emit=["boring-task-state"])
    command = [str(part) for part in run.call_args.args]
    assert command[:3] == ["/bin/uv", "pip", "compile"]
    assert "--generate-hashes" in command and "--universal" in command
    assert command[command.index("--python-version") + 1] == "3.12"
    assert command[command.index("--no-emit-package") + 1] == "boring-task-state"
    assert command[command.index("--upgrade-package") + 1] == "boring-registry"
    assert command.count("--find-links") == 2


def test_check_environment_installs_locks_with_hashes_then_the_packages(tmp_path):
    with mock.patch.object(develop, "run") as run:
        python = develop.environment("uv", "python3", tmp_path / "venv", [tmp_path / "requirements.lock"],
                                     tmp_path, [tmp_path / "app"], tmp_path / "constraints.txt", ["pytest==9"])
    calls = [[str(part) for part in call.args] for call in run.call_args_list]
    assert python == tmp_path / "venv/bin/python"
    assert "--require-hashes" in calls[1]
    assert "--no-deps" in calls[2] and str(tmp_path / "app") in calls[2]
    assert calls[3][-1] == "pytest==9"


def test_linux_platforms_include_every_compatible_x86_64_tag():
    assert develop.LINUX_GLIBC_FLOOR == (2, 39)
    assert develop.LINUX_PLATFORMS == (
        "manylinux_2_39_x86_64", "manylinux_2_38_x86_64", "manylinux_2_37_x86_64",
        "manylinux_2_36_x86_64", "manylinux_2_35_x86_64", "manylinux_2_34_x86_64",
        "manylinux_2_33_x86_64", "manylinux_2_32_x86_64", "manylinux_2_31_x86_64",
        "manylinux_2_30_x86_64", "manylinux_2_29_x86_64", "manylinux_2_28_x86_64",
        "manylinux_2_27_x86_64", "manylinux_2_26_x86_64", "manylinux_2_25_x86_64",
        "manylinux_2_24_x86_64", "manylinux_2_23_x86_64", "manylinux_2_22_x86_64",
        "manylinux_2_21_x86_64", "manylinux_2_20_x86_64", "manylinux_2_19_x86_64",
        "manylinux_2_18_x86_64", "manylinux_2_17_x86_64",
        "manylinux2014_x86_64", "linux_x86_64")


def test_wheelhouse_passes_each_compatible_tag_to_pip(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("UV", "/bin/uv")
    out = tmp_path / "wheels"
    out.mkdir()
    with mock.patch.object(develop, "pip_environment", return_value=Path("/fake/python")), \
         mock.patch.object(develop, "target_lock", return_value=tmp_path / "target.lock"), \
         mock.patch.object(develop.subprocess, "run") as run:
        develop.wheelhouse(repo, out, sources=[], lock_file=tmp_path / "lock",
                           common_wheels=tmp_path, python_version="3.14", constraints=tmp_path / "constraints")
    command = run.call_args.args[0]
    tags = [command[i + 1] for i, value in enumerate(command) if value == "--platform"]
    assert tuple(tags) == develop.LINUX_PLATFORMS


def test_arguments_for_a_build_and_for_a_check():
    build = develop.arguments("Build.", build=True).parse_args(["--common-wheels", "d", "--out", "o"])
    assert (build.python_version, build.allow_dirty) == ("3.14", False)
    parser = develop.arguments("Build.", build=True)
    assert parser.parse_args(["--common-wheels", "d", "--out", "o", "--python-version", "3.12"]).python_version == "3.12"
    for unsupported in ("3.10", "3.11"):
        with pytest.raises(SystemExit):
            parser.parse_args(["--common-wheels", "d", "--out", "o", "--python-version", unsupported])
    check = develop.arguments("Check.").parse_args(["--common-wheels", "d"])
    assert check.common_wheels == Path("d")
