"""A foreground command that reaches its time limit reports timed_out, not an unknown outcome."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from boring_worker_kit.execution import EnvironmentExecutor, FOREGROUND_RESULT_SLACK_SECONDS
from boring_worker_kit.host_limits import HostLimits
from boring_worker_kit.settings import EnvironmentSettings


class SlowRunner:
    """Like the task runner: uses the whole timeout, then needs a moment to stop and reply."""

    def __init__(self):
        self.timeouts = []

    async def execute(self, command, cwd, timeout, stop, *, job_id=None, on_output=None):
        self.timeouts.append(timeout)
        await asyncio.sleep(timeout + 0.3)
        return {"cwd": cwd, "exit_code": None, "timed_out": True, "stdout": "partial", "stderr": "",
                "stdout_truncated": False, "stderr_truncated": False}


class ForegroundDeadlineTest(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_inside_the_budget_reports_the_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = EnvironmentSettings(
                name="t", default_cwd=root, allowed_roots=(root,), task_socket_path=root / "task.sock",
                task_unit_prefix="example-task-t", task_cgroup_parent=Path("/system.slice/system-example.slice"))
            # Foreground budget 12 s: half (6 s) is reserved for cleanup; of the
            # remaining 6 s the runner slack keeps up to half; the runner then
            # needs 0.3 s beyond the command's own timeout.
            executor = EnvironmentExecutor(settings, HostLimits(1, 12))
            runner = executor._tasks = SlowRunner()
            result = await executor.execute("sleep 999", None, None)
            self.assertTrue(result["timed_out"])
            self.assertEqual(result["stdout"], "partial")
            self.assertLessEqual(runner.timeouts[0], 6 - min(FOREGROUND_RESULT_SLACK_SECONDS, 3) + 0.01)


if __name__ == "__main__":
    unittest.main()
