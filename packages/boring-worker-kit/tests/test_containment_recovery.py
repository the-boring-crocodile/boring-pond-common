"""Admission recovery contracts; real directory handles, synthetic task lifecycles."""
import asyncio
import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from boring_worker_kit.execution import EnvironmentExecutor
from boring_worker_kit.host_limits import HostLimits
from boring_worker_kit.settings import EnvironmentSettings
from boring_worker_kit.task_client import ContainmentError, TaskCgroup


class RecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        settings = EnvironmentSettings(name="test", default_cwd=self.root, allowed_roots=(self.root,),
                                       task_socket_path=None, output_limit_bytes=128, task_unit_prefix='boring-crocodile-task-pond', task_cgroup_parent=Path('/system.slice/system-crocodilepond.slice'))
        self.executor = EnvironmentExecutor(settings, HostLimits(3, None))
        self.calls = []
        self.failures = {}
        outer = self

        class Tasks:
            async def execute(self, command, cwd, timeout, stop, **kwargs):
                outer.calls.append(command)
                if command in outer.failures:
                    raise outer.failures[command]
                return {"stdout": command, "exit_code": 0}

        self.executor._tasks = Tasks()

    def group(self, name):
        path = self.root/name
        path.mkdir()
        (path/"cgroup.events").write_text("populated 1\n")
        group = TaskCgroup(path, directory_fd=os.open(
            path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))
        self.addCleanup(group.close)
        return group

    async def fail(self, command, group=None):
        self.failures[command] = ContainmentError("unconfirmed cleanup", group=group)
        with self.assertRaises(ContainmentError):
            await self.executor.execute(command, None, 1, job_id=command)

    async def blocked(self):
        before = list(self.calls)
        with self.assertRaisesRegex(ContainmentError, "admission disabled"):
            await self.executor.execute("must-not-start", None, 1)
        self.assertEqual(self.calls, before)

    async def test_population_blocks_then_late_empty_recovers_without_replay(self):
        group = self.group("failed")
        await self.fail("original", group)
        await self.blocked()
        (group.path/"cgroup.events").write_text("populated 0\n")
        result = await self.executor.execute("fresh", None, 1)
        self.assertEqual(result["stdout"], "fresh")
        self.assertEqual(self.calls, ["original", "fresh"])
        self.assertTrue(group._closed)

    async def test_unreadable_or_malformed_state_cannot_restore_admission(self):
        group = self.group("failed")
        await self.fail("original", group)
        for error in (OSError(errno.EACCES, "unreadable"), OSError(errno.EIO, "broken")):
            with patch.object(group, "is_empty", side_effect=error):
                await self.blocked()
        for raw in ("", "frozen 0\n", "populated 2\n", "populated\n"):
            (group.path/"cgroup.events").write_text(raw)
            await self.blocked()
        (group.path/"cgroup.events").write_text("populated 0\n")
        self.assertEqual((await self.executor.execute("fresh", None, 1))["exit_code"], 0)

    async def test_unknown_identity_failure_remains_blocked(self):
        await self.fail("unknown")
        for _ in range(2):
            await self.blocked()

    async def test_path_reuse_does_not_observe_replacement_empty_group(self):
        group = self.group("failed")
        await self.fail("original", group)
        original = self.root/"retained-original"
        group.path.rename(original)
        group.path.mkdir()
        (group.path/"cgroup.events").write_text("populated 0\n")
        await self.blocked()
        (original/"cgroup.events").write_text("populated 0\n")
        await self.executor.execute("fresh", None, 1)
        self.assertEqual(self.calls, ["original", "fresh"])

    async def test_removed_original_recovers_even_when_replacement_is_populated(self):
        group = self.group("failed")
        await self.fail("original", group)
        (group.path/"cgroup.events").unlink()
        group.path.rmdir()
        group.path.mkdir()
        replacement = group.path/"cgroup.events"
        replacement.write_text("populated 1\n")
        await self.executor.execute("fresh", None, 1)
        self.assertEqual(replacement.read_text(), "populated 1\n")
        self.assertTrue(group._closed)

    async def test_concurrent_failures_all_must_clear_neighbor_keeps_running(self):
        groups = {name: self.group(name) for name in ("first", "second")}
        started = asyncio.Event()
        release = asyncio.Event()
        count = 0
        calls = self.calls

        class Tasks:
            async def execute(self, command, cwd, timeout, stop, **kwargs):
                nonlocal count
                calls.append(command)
                count += 1
                if count == 3:
                    started.set()
                await started.wait()
                if command in groups:
                    raise ContainmentError("unconfirmed cleanup", group=groups[command])
                if command == "neighbor":
                    await release.wait()
                return {"exit_code": 0}

        self.executor._tasks = Tasks()
        tasks = [asyncio.create_task(self.executor.execute(
            name, None, 1, job_id=name)) for name in ("first", "second", "neighbor")]
        try:
            results = await asyncio.gather(*tasks[:2], return_exceptions=True)
            self.assertTrue(all(isinstance(result, ContainmentError) for result in results))
            await self.blocked()
            (groups["first"].path/"cgroup.events").write_text("populated 0\n")
            await self.blocked()
            self.assertTrue(groups["first"]._closed)
            self.assertFalse(tasks[2].done())
            (groups["second"].path/"cgroup.events").write_text("populated 0\n")
            self.assertEqual((await self.executor.execute("fresh", None, 1))["exit_code"], 0)
            self.assertFalse(tasks[2].done())
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(calls, ["first", "second", "neighbor", "fresh"])


class PinnedCgroupTest(unittest.IsolatedAsyncioTestCase):
    async def test_removed_node_errors_and_closed_handle_never_rebind(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            group = TaskCgroup(path, directory_fd=descriptor)
            try:
                for code in (errno.ENOENT, errno.ENODEV):
                    with patch("boring_worker_kit.task_client.os.open",
                               side_effect=OSError(code, "removed")):
                        self.assertTrue(group.is_empty())
            finally:
                group.close()
            with self.assertRaisesRegex(RuntimeError, "closed"):
                group.is_empty()
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    async def test_wait_and_recovery_share_population_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path/"cgroup.events").write_text("populated 1\n")
            group = TaskCgroup(path, directory_fd=os.open(
                path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))
            try:
                with self.assertRaises(ContainmentError):
                    await group.wait_empty(.01)
                (path/"cgroup.events").write_text("populated 0\n")
                await group.wait_empty(.01)
            finally:
                group.close()
