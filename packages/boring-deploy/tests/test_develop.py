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


def test_arguments_for_a_build_and_for_a_check():
    build = develop.arguments("Build.", build=True).parse_args(["--common-wheels", "d", "--out", "o"])
    assert (build.python_version, build.allow_dirty) == ("3.14", False)
    check = develop.arguments("Check.").parse_args(["--common-wheels", "d"])
    assert check.common_wheels == Path("d")
