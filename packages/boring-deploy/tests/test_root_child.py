"""Root child boundaries ignore caller cwd, Python paths and tool PATH."""
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import boring_deploy


def context():
    return boring_deploy.Context(None, Path("/unused"), None, lambda line: None)


def test_context_python_cannot_import_from_operator_cwd_or_pythonpath(tmp_path, monkeypatch):
    (tmp_path / "json.py").write_text("raise RuntimeError('operator module executed')\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    result = context().run(sys.executable, "-c", "import json, os; print(os.getcwd())")
    assert result.stdout.strip() == "/"


def test_context_uses_fixed_tool_paths_and_cwd_even_with_user_switch(tmp_path, monkeypatch):
    fake = tmp_path / "systemctl"
    fake.write_text("#!/bin/sh\nexit 99\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    calls = []
    def run(command, **options):
        calls.append((command, options))
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", run)
    context().run("systemctl", "is-active", "example.service", user="pond")
    command, options = calls.pop()
    assert command == ["/usr/sbin/runuser", "-u", "pond", "--", "/usr/bin/systemctl", "is-active", "example.service"]
    assert options["cwd"] == "/"
    assert options["env"]["PATH"] == "/usr/sbin:/usr/bin:/sbin:/bin"


@pytest.mark.parametrize("prefix", ["", "/usr/bin/env -S "])
def test_python_console_script_is_isolated_too(tmp_path, monkeypatch, prefix):
    script = tmp_path / "admin"
    script.write_text("#!" + prefix + sys.executable + "\nimport json, os; print(os.getcwd())\n")
    script.chmod(0o755)
    (tmp_path / "json.py").write_text("raise RuntimeError('operator module executed')\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    result = boring_deploy.root_child([str(script)], check=True, capture_output=True, text=True)
    assert result.stdout.strip() == "/"


def test_spawned_python_uses_the_same_boundary():
    spawn = Mock(return_value="child")
    assert boring_deploy.root_child([sys.executable, "-c", "pass"], spawn=spawn) == "child"
    command = spawn.call_args.args[0]
    assert command == [sys.executable, "-I", "-B", "-c", "pass"]
    assert spawn.call_args.kwargs["cwd"] == "/"


@pytest.mark.parametrize("command", [[], ["./local-program"], ["missing-pond-test-program"]])
def test_no_implicit_local_executable(command):
    with pytest.raises(boring_deploy.Failure):
        boring_deploy.root_child(command)


def test_root_child_cannot_override_its_working_directory(tmp_path):
    with pytest.raises(boring_deploy.Failure, match="working directory"):
        boring_deploy.root_child([sys.executable, "-c", "pass"], cwd=str(tmp_path))


def test_python_isolation_is_applied_inside_runuser(monkeypatch):
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(subprocess, "run", run)
    context().run(sys.executable, "-c", "pass", user="pond")
    assert run.call_args.args[0] == ["/usr/sbin/runuser", "-u", "pond", "--",
                                    sys.executable, "-I", "-B", "-c", "pass"]
