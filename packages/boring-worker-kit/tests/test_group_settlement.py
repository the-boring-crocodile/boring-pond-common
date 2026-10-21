"""Local development cleanup cannot use pipe EOF as descendant exit."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from boring_worker_kit.execution import EnvironmentExecutor
from boring_worker_kit.host_limits import HostLimits
from boring_worker_kit.settings import EnvironmentSettings
from boring_worker_kit.process import ProcessCleanupError, run_process, wait_group_exit
from boring_worker_kit.task_client import ContainmentError


class GroupSettlementTest(unittest.IsolatedAsyncioTestCase):
    async def test_exit_wait_retains_ownership_until_observation_clears(self):
        running = True
        with patch("boring_worker_kit.process.group_running", side_effect=lambda _: running):
            waiting = asyncio.create_task(wait_group_exit(123))
            try:
                await asyncio.sleep(.025)
                self.assertFalse(waiting.done())
                running = False
                await asyncio.wait_for(waiting, 1)
            finally:
                running = False
                await waiting

    async def test_lingering_group_is_failure_not_success(self):
        with patch("boring_worker_kit.process.group_running", return_value=True), \
                patch("boring_worker_kit.process.GROUP_EXIT_SECONDS", .2):
            with self.assertRaisesRegex(ProcessCleanupError, "not confirmed"):
                await wait_group_exit(123)

    async def test_unreadable_members_are_unknown(self):
        with patch("boring_worker_kit.process.group_running", side_effect=PermissionError):
            with self.assertRaisesRegex(ProcessCleanupError, "could not be observed"):
                await wait_group_exit(123)

    async def test_unconfirmed_local_cleanup_blocks_next_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = EnvironmentSettings(name="fixture", default_cwd=root, allowed_roots=(root,),
                                           task_socket_path=None, output_limit_bytes=1024, task_unit_prefix='boring-crocodile-task-pond', task_cgroup_parent=Path('/system.slice/system-crocodilepond.slice'))
            executor = EnvironmentExecutor(settings, HostLimits(1, None))
            with patch("boring_worker_kit.execution.run_process",
                       new=AsyncMock(side_effect=ProcessCleanupError("unconfirmed"))) as run:
                with self.assertRaises(ContainmentError):
                    await executor.execute("synthetic", None, 1, job_id="one")
                with self.assertRaisesRegex(ContainmentError, "admission disabled"):
                    await executor.execute("must-not-run", None, 1, job_id="two")
                self.assertEqual(run.await_count, 1)

    async def test_systemd_runner_does_not_add_proc_group_observer(self):
        with patch("boring_worker_kit.process.group_running",
                   side_effect=AssertionError("systemd owns containment")):
            result = await run_process("printf ok", "/tmp", 1, 64, asyncio.Event(),
                                       systemd_contained=True)
        self.assertEqual(result["stdout"], "ok")

    async def assert_pid_gone(self, pid: int) -> None:
        deadline = asyncio.get_running_loop().time() + 2
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            if asyncio.get_running_loop().time() >= deadline:
                self.fail(f"background process {pid} survived group cleanup")
            await asyncio.sleep(.02)

    async def test_real_group_timeout_kills_background_member(self):
        # 5 s: long enough for bash to start the child and print its pid on a
        # loaded host, far short of the child's 30 s.
        result = await run_process("sleep 30 & echo $!; wait", "/tmp", 5, 1024,
                                   asyncio.Event())
        self.assertTrue(result["timed_out"])
        self.assertNotEqual(result["exit_code"], 0)
        self.assertLess(result["duration_seconds"], 10)
        await self.assert_pid_gone(int(result["stdout"].strip()))

    async def test_leader_exits_normally_and_its_background_child_is_ended(self):
        result = await run_process("sleep 30 & echo $!; exit 0", "/tmp", 30, 1024, asyncio.Event())
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["exit_code"], 0)
        self.assertLess(result["duration_seconds"], 10)
        await self.assert_pid_gone(int(result["stdout"].strip()))

    async def test_real_group_stop_kills_background_member(self):
        stop = asyncio.Event()
        # Stop once the child's pid is out, not after a fixed delay.
        result = await run_process("sleep 30 & echo $!; wait", "/tmp", 30, 1024, stop,
                                   on_output=lambda snap: snap["stdout"].strip() and stop.set())
        self.assertFalse(result["timed_out"])
        self.assertNotEqual(result["exit_code"], 0)
        self.assertLess(result["duration_seconds"], 10)
        await self.assert_pid_gone(int(result["stdout"].strip()))
