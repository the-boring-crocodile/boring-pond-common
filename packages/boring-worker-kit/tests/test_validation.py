"""Plain configuration validation stays usable without a schema/native runtime."""
import os
from pathlib import Path
import subprocess
import sys

from boring_worker_kit import inputs, validation


def test_existing_input_exports_keep_the_same_validator_owner():
    for name in ("table", "text", "integer", "strings"):
        assert getattr(inputs, name) is getattr(validation, name)
    assert inputs.MAX_JOB_SECONDS == validation.MAX_JOB_SECONDS == 86400.0


def test_configuration_validation_does_not_import_pydantic():
    source = str(Path(validation.__file__).resolve().parents[1])
    code = """
import sys
sys.modules["pydantic"] = None
from boring_worker_kit.validation import table, integer
from boring_worker_kit.file_policy import FilePolicy
from boring_worker_kit.settings import EnvironmentSettings
from boring_worker_kit.host_limits import HostLimits, TASK_OVERHEAD_SECONDS
assert HostLimits(1, None).job_max_seconds == 86400.0
assert TASK_OVERHEAD_SECONDS == 10
assert table({"a": 1}, "sample", {"a"}) == {"a": 1}
assert not FilePolicy.parse(None).roots
try:
    integer(True, "count", 1)
except ValueError:
    pass
else:
    raise AssertionError("boolean became an integer")
EnvironmentSettings.from_mapping(dict(name="pond", default_cwd="/home/pond",
    allowed_roots=["/home/pond"], task_socket_path="/run/pond/task.sock",
    task_unit_prefix="boring-test-task-pond", task_cgroup_parent="/system.slice/system-pond.slice"))
"""
    result = subprocess.run([sys.executable, "-c", code],
                            env=dict(os.environ, PYTHONPATH=source), capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
