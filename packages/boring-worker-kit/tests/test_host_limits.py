import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from boring_worker_kit.execution import prepare_command
from boring_worker_kit.host_limits import HostLimits, USEC_INFINITY, read_host_limits
from boring_worker_kit.settings import EnvironmentSettings
from boring_worker_kit.outcomes import Refused


class HostLimitsTest(unittest.TestCase):
    def settings(self, root):
        return EnvironmentSettings(
            "pond", root, (root,), root / "task.sock", "acme-task-pond",
            Path("/system.slice/system-acme.slice"), 1024)

    def test_caller_can_own_process_launch_without_losing_the_deadline(self):
        for finish, expires in ((1.5, False), (3.1, True)):
            with self.subTest(expires=expires), tempfile.TemporaryDirectory() as directory:
                replies = iter(["2", 'o "/org/freedesktop/systemd1/unit/task_2eservice"', "t 90000000"])
                timeouts = []
                def run(command, *, timeout):
                    timeouts.append(timeout)
                    return next(replies)
                with patch("boring_worker_kit.host_limits._run", side_effect=AssertionError("caller bypassed")), \
                        patch("boring_worker_kit.host_limits.time.monotonic",
                              side_effect=[0, 0, .5, .5, 1, 1, finish]):
                    if expires:
                        with self.assertRaisesRegex(TimeoutError, "startup budget"):
                            read_host_limits(self.settings(Path(directory)), run=run)
                    else:
                        limits = read_host_limits(self.settings(Path(directory)), run=run)
                        self.assertEqual(limits, HostLimits(2, 80))
                self.assertEqual(timeouts, [3, 2.5, 2])

    def test_loaded_finite_host_policy_and_command_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(Path(directory))
            with patch("boring_worker_kit.host_limits._run", side_effect=[
                "2", 'o "/org/freedesktop/systemd1/unit/task_2eservice"', "t 90000000"
            ]) as run:
                limits = read_host_limits(settings)
            self.assertEqual(limits.public(), {
                "exec_timeout_seconds": 80.0,
                "job_timeout_seconds": 80.0,
                "task_slots": 2,
            })
            self.assertIn("acme-task-pond.socket", run.call_args_list[0].args[0])
            self.assertEqual(prepare_command(settings, "true", None, None, limits)[1], 80)
            self.assertEqual(prepare_command(settings, "true", None, 1000, limits, is_job=True)[1], 80)
            with self.assertRaises(Refused):
                prepare_command(settings, "true", None, 0, limits, is_job=True)

    def test_uncapped_host_reports_the_fixed_job_maximum(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(Path(directory))
            with patch("boring_worker_kit.host_limits._run", side_effect=[
                "1", 'o "/org/freedesktop/systemd1/unit/task_2eservice"',
                f"t {USEC_INFINITY}",
            ]):
                limits = read_host_limits(settings)
            self.assertEqual(limits.public(), {
                "exec_timeout_seconds": 103.0,
                "job_timeout_seconds": 86400.0,
                "task_slots": 1,
            })
            self.assertEqual(prepare_command(settings, "true", None, None, limits, is_job=True)[1], 120)
            for zero_job in (True, False):
                with self.assertRaises(Refused):
                    prepare_command(settings, "true", None, 0, limits, is_job=zero_job)

    def test_unknown_capacity_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("boring_worker_kit.host_limits._run", return_value="0"):
                with self.assertRaisesRegex(ValueError, "capacity"):
                    read_host_limits(self.settings(Path(directory)))

    def test_inspection_calls_share_one_total_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("boring_worker_kit.host_limits.time.monotonic",
                       side_effect=[0, 0, .5, .5, 1, 1, 3.1]):
                with patch("boring_worker_kit.host_limits._run", side_effect=[
                    "2", 'o "/org/freedesktop/systemd1/unit/task_2eservice"', "t 90000000"
                ]) as run:
                    with self.assertRaisesRegex(TimeoutError, "startup budget"):
                        read_host_limits(self.settings(Path(directory)))
            self.assertEqual([call.kwargs["timeout"] for call in run.call_args_list], [3, 2.5, 2])
