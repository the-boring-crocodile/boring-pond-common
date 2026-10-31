import asyncio
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from boring_worker_kit.execution import EnvironmentExecutor, ForegroundInterrupted, prepare_command
from boring_worker_kit.host_limits import HostLimits
from boring_worker_kit.process import ProcessOutput, run_process
from boring_worker_kit.settings import EnvironmentSettings
from boring_worker_kit.outcomes import Refusal


class ExecutionTest(unittest.TestCase):
    def make_settings(self, root: Path) -> EnvironmentSettings:
        return EnvironmentSettings(
            name="pond",
            default_cwd=root,
            allowed_roots=(root,),
            task_socket_path=None,  # in-process, no task runner
            task_unit_prefix="acme-task-pond",
            task_cgroup_parent=Path("/system.slice/system-acme.slice"),
            output_limit_bytes=1024,
        )

    def test_job_timeout_default_maximum_and_host_cap(self):
        settings = self.make_settings(Path("/tmp"))
        uncapped = HostLimits(1, None)
        bounded = HostLimits(1, 10)
        for invalid in (False, True, "0", float("nan"), float("inf"), 0, 0.05, 86401):
            with self.subTest(invalid=invalid), self.assertRaises(Refusal):
                prepare_command(settings, "true", None, invalid, uncapped, is_job=True)
        self.assertEqual(prepare_command(settings, "true", None, None, uncapped, is_job=True)[1], 120)
        self.assertEqual(prepare_command(settings, "true", None, 86400, uncapped, is_job=True)[1], 86400)
        self.assertEqual(prepare_command(settings, "true", None, 20, bounded, is_job=True)[1], 10)
        for zero_cap in (bounded, uncapped):
            with self.assertRaises(Refusal):
                prepare_command(settings, "true", None, 0, zero_cap, is_job=False)

    def test_result_carries_no_echo_or_counters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            result = asyncio.run(
                EnvironmentExecutor(self.make_settings(root), HostLimits(1, None)).execute(
                    "printf ok",
                    None,
                    None,
                )
            )
            self.assertFalse({"environment", "command", "resources", "cleanup"} & set(result))
            self.assertEqual(result["stdout"], "ok")
            self.assertGreater(result["effective_timeout_seconds"], 0)
            self.assertLessEqual(result["effective_timeout_seconds"], 103)

    def test_rejects_cwd_outside_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with self.assertRaises(Refusal):
                asyncio.run(
                    EnvironmentExecutor(self.make_settings(root), HostLimits(1, None)).execute(
                        "true",
                        "/",
                        None,
                    )
                )


class ForegroundDeadlineTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = ExecutionTest().make_settings(self.root)

    async def test_running_output_flushes_quiet_burst_and_cancels_timer(self):
        updates = []
        output = ProcessOutput(64, on_output=updates.append)
        output.pipe_data_received(1, b"first\n")
        output.pipe_data_received(2, b"last warning\n")
        self.assertEqual(updates[-1]["stderr"], "")
        await asyncio.sleep(.15)
        self.assertEqual(updates[-1]["stderr"], "last warning\n")
        self.assertIsNone(output.pending_snapshot)
        output.pipe_data_received(1, b"tail\n")
        output.publish_snapshot()
        self.assertIsNone(output.pending_snapshot)
        self.assertEqual(updates[-1]["stdout"], "first\ntail\n")

    async def test_exceptional_process_exit_cancels_quiet_burst(self):
        updates = []
        pending = []
        async def fail_cleanup(_, output):
            output.last_snapshot = asyncio.get_running_loop().time()
            output.pipe_data_received(2, b"trailing warning")
            pending.append(output.pending_snapshot)
            raise RuntimeError("synthetic cleanup failure")
        with patch("boring_worker_kit.process.stop_group", side_effect=fail_cleanup):
            with self.assertRaisesRegex(RuntimeError, "synthetic cleanup failure"):
                await run_process("printf first; sleep .02; printf warning >&2; sleep .02",
                                  str(self.root), 2, 1024, asyncio.Event(),
                                  on_output=updates.append)
        self.assertEqual(len(pending), 1)
        self.assertIsNotNone(pending[0])
        self.assertTrue(pending[0].cancelled())
        count = len(updates)
        await asyncio.sleep(.15)
        self.assertEqual(len(updates), count)

    async def test_full_occupancy_times_out_without_later_dispatch(self):
        executor = EnvironmentExecutor(self.settings, HostLimits(8, .2))
        started = []
        release = asyncio.Event()
        full = asyncio.Event()
        class Tasks:
            async def execute(inner, command, cwd, timeout, stop, **kwargs):
                started.append(command)
                if len(started) == 8:
                    full.set()
                await release.wait()
                return {"stdout": command}
        executor._tasks = Tasks()
        jobs = [asyncio.create_task(executor.execute(str(n), None, 0.1, job_id=str(n)))
                for n in range(8)]
        try:
            await asyncio.wait_for(full.wait(), 1)
            with self.assertRaisesRegex(Refusal, "^not_admitted: foreground admission timeout; command not started$"):
                await executor.execute("must-not-run", None, 0.1)
            release.set()
            await asyncio.gather(*jobs)
            self.assertNotIn("must-not-run", started)
            self.assertEqual((await executor.execute("after-drain", None, 0.1))["stdout"],
                             "after-drain")
        finally:
            release.set()
            await asyncio.gather(*jobs, return_exceptions=True)

    async def test_deadline_retains_slot_through_cleanup(self):
        executor = EnvironmentExecutor(self.settings, HostLimits(1, .2))
        started = asyncio.Event()
        stopping = asyncio.Event()
        clean = asyncio.Event()
        follower_started = asyncio.Event()
        class Tasks:
            async def execute(inner, command, cwd, timeout, stop, **kwargs):
                if command == "follower":
                    follower_started.set()
                    return {}
                started.set()
                await stop.wait()
                stopping.set()
                await clean.wait()
                return {}
        executor._tasks = Tasks()
        foreground = asyncio.create_task(executor.execute("running", None, .1))
        follower = None
        try:
            await started.wait()
            await asyncio.wait_for(stopping.wait(), 1)
            self.assertFalse(foreground.done())
            follower = asyncio.create_task(executor.execute("follower", None, .1, job_id="job"))
            await asyncio.sleep(.01)
            self.assertFalse(follower_started.is_set())
            clean.set()
            with self.assertRaisesRegex(ForegroundInterrupted, "request deadline exceeded.*command may have started"):
                await foreground
            await asyncio.wait_for(follower, 1)
        finally:
            clean.set()
            await asyncio.gather(foreground, *([follower] if follower else []),
                                 return_exceptions=True)

    async def test_admission_time_reduces_passed_command_budget(self):
        executor = EnvironmentExecutor(self.settings, HostLimits(1, .6))
        budgets = []
        class Tasks:
            async def execute(inner, command, cwd, timeout, stop, **kwargs):
                budgets.append(timeout)
                return {}
        executor._tasks = Tasks()
        await executor._slots.acquire()
        foreground = asyncio.create_task(executor.execute("queued", None, .6))
        await asyncio.sleep(.08)
        executor._slots.release()
        await foreground
        self.assertGreater(budgets[0], 0)
        self.assertLess(budgets[0], .25)

    async def test_internal_timeout_is_not_misreported_as_request_deadline(self):
        executor = EnvironmentExecutor(self.settings, HostLimits(1, 10))
        class Tasks:
            async def execute(inner, *args, **kwargs):
                raise TimeoutError("runner-specific failure")
        executor._tasks = Tasks()
        with self.assertRaisesRegex(TimeoutError, "runner-specific failure"):
            await executor.execute("true", None, 1)

class BoundedOutputBackportTest(unittest.IsolatedAsyncioTestCase):
    async def test_head_tail_and_totals_independent_of_chunk_boundaries(self):
        from boring_worker_kit.process import BoundedOutput
        payload = b"FIRST ERROR\n" + b"x" * 100000 + b"\nFINAL SUMMARY"
        for chunk_size in (1, 17, 4096, len(payload)):
            output = BoundedOutput(128)
            for offset in range(0, len(payload), chunk_size):
                output.append(payload[offset:offset+chunk_size])
                self.assertLessEqual(len(output.head) + len(output.tail), 128)
            text = output.value()
            self.assertTrue(text.startswith("FIRST ERROR\n"))
            self.assertTrue(text.endswith("\nFINAL SUMMARY"))
            self.assertIn("output truncated", text)
            self.assertEqual(output.total, len(payload))
            self.assertLessEqual(len(text.encode()), 128)
        for limit in range(1, 12):
            output = BoundedOutput(limit)
            output.append(b"a" * 100)
            self.assertLessEqual(len(output.value().encode()), limit)

    async def test_real_process_final_output_and_snapshot_keep_summary(self):
        import shlex
        import sys
        snapshots = []
        script = "import sys; print('first'); print('x'*10000); print('summary'); print('last-error', file=sys.stderr)"
        result = await run_process(shlex.join([sys.executable, "-c", script]), "/tmp", 5, 128,
                                   asyncio.Event(), on_output=snapshots.append)
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["stdout"].startswith("first\n"))
        self.assertTrue(result["stdout"].endswith("summary\n"))
        self.assertEqual(result["stderr"], "last-error\n")
        self.assertEqual(snapshots[-1]["stdout"], result["stdout"])
        self.assertEqual(result["stdout_bytes"], 10015)
        self.assertTrue(result["stdout_truncated"])
