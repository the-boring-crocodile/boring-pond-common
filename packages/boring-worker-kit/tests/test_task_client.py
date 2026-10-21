import asyncio
import errno
from pathlib import Path
import os
import socket
import tempfile
import unittest
from unittest.mock import patch

from boring_worker_kit.outcomes import Refusal
from boring_worker_kit.execution import EnvironmentExecutor
from boring_worker_kit.host_limits import HostLimits
from boring_worker_kit.framing import read_frame, write_frame
from boring_worker_kit.settings import EnvironmentSettings
from boring_worker_kit import task_client
from boring_worker_kit.task_client import ContainmentError, TaskCgroup, TaskClient


class TaskClientTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.settings=EnvironmentSettings(name='test', default_cwd=self.root, allowed_roots=(self.root,),
            task_socket_path=self.root/'task.sock', task_unit_prefix='boring-crocodile-task-test',
            task_cgroup_parent=Path('/system.slice/system-crocodilepond.slice'), output_limit_bytes=128)
        self.cleaned=asyncio.Event()
        self.sent=asyncio.Event()
        self.requests=[]
        self.hold_response=False
        self.response_error=False
        self.progress_snapshot=None
        self.progress_sent=asyncio.Event()
        self.release_result=asyncio.Event()
        self.client_eof=asyncio.Event()
        self.hello={'version':1,'pid':os.getpid(),'cgroup':'/system.slice/system-crocodilepond.slice/task-test@one.service'}
        self.errors=[]
        self.handlers=set()
        async def handler(reader,writer):
            task=asyncio.current_task()
            self.handlers.add(task)
            try:
                await write_frame(writer,self.hello)
                request=await read_frame(reader)
                self.requests.append(request)
                self.sent.set()
                if self.progress_snapshot is not None:
                    await write_frame(writer, {"type": "progress", "output": self.progress_snapshot})
                    self.progress_sent.set()
                    await self.release_result.wait()
                if self.response_error:
                    (self.root/'memory.events').write_text('max 4\noom_kill 1\n')
                    await write_frame(writer, {'ok': False, 'error': 'runner failure'})
                elif not self.hold_response:
                    await write_frame(writer,{'ok':True,'result':{'exit_code':0,'stdout':'ok','stderr':''}})
                await reader.read()
                self.client_eof.set()  # the client closed its end
            except (asyncio.IncompleteReadError,ConnectionError):
                pass
            finally:
                writer.close()
                await writer.wait_closed()
                self.handlers.remove(task)
        self.server=await asyncio.start_unix_server(handler,self.settings.task_socket_path)
        self.client=TaskClient(self.settings)
        # Tests mock only trusted endpoint/OS containment; exercise actual framed sockets.
        async def connect():
            return await asyncio.open_unix_connection(self.settings.task_socket_path)
        self.client._connect=connect
        class Group(TaskCgroup):
            def is_empty(inner):
                return not self.errors

            async def wait_empty(inner):
                await self.cleaned.wait()
                if self.errors:
                    raise self.errors[0]
        self.observer=patch.object(TaskCgroup,'observe',
                                  side_effect=lambda *args: Group(self.root/'task-group'))
        self.observer.start()
        self.tasks=[]

    async def asyncTearDown(self):
        # An assertion failure must not strand a handler awaiting the final result.
        self.release_result.set()
        self.cleaned.set()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        self.server.close()
        await self.server.wait_closed()
        await asyncio.gather(*list(self.handlers),return_exceptions=True)
        self.observer.stop()
        self.temp.cleanup()

    def launch(self,stop=None):
        task=asyncio.create_task(self.client.execute('true',str(self.root),1,stop or asyncio.Event()))
        self.tasks.append(task)
        return task

    async def test_boolean_protocol_version_never_dispatches(self):
        self.hello["version"] = True
        self.cleaned.set()
        with self.assertRaisesRegex(ValueError, "unsupported task protocol"):
            await self.launch()
        self.assertEqual(self.requests, [])

    async def test_managed_job_identity_reaches_runner_request(self):
        job_id = "a" * 32
        task = asyncio.create_task(self.client.execute(
            "true", str(self.root), 1, asyncio.Event(), job_id=job_id))
        self.tasks.append(task)
        await self.sent.wait()
        self.assertEqual(self.requests[0]["job_id"], job_id)
        self.cleaned.set()
        self.assertEqual((await task)["exit_code"], 0)

    async def test_backported_process_snapshot_crosses_task_socket(self):
        from boring_worker_kit.process import ProcessOutput
        output = ProcessOutput(128)
        output.pipe_data_received(1, b"first error" + b"x" * 5000 + b"last summary")
        output.pipe_data_received(2, b"warning")
        self.progress_snapshot = output.snapshot()
        updates = []
        self.release_result.set()
        self.cleaned.set()
        result = await self.client.execute("true", str(self.root), 1, asyncio.Event(),
                                           job_id="a" * 32, on_output=updates.append)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(updates[0]["stdout_bytes"], 5023)
        self.assertTrue(updates[0]["stdout"].startswith("first error"))
        self.assertTrue(updates[0]["stdout"].endswith("last summary"))

    async def test_valid_progress_without_optional_callback_is_consumed(self):
        self.progress_snapshot = {"stdout": "hello", "stderr": "",
                                  "stdout_truncated": False, "stderr_truncated": False}
        self.release_result.set()
        self.cleaned.set()
        result = await self.client.execute("true", str(self.root), 1, asyncio.Event(),
                                           job_id="a" * 32)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"], "ok")

    async def test_malformed_progress_without_callback_is_still_refused(self):
        self.progress_snapshot = {"stdout": "hello"}
        self.release_result.set()
        self.cleaned.set()
        with self.assertRaisesRegex(ValueError, "invalid task progress"):
            await self.client.execute("true", str(self.root), 1, asyncio.Event(),
                                      job_id="a" * 32)

    async def test_progress_byte_counts_are_strict_and_paired(self):
        self.release_result.set()
        self.cleaned.set()
        for extra in ({"stdout_bytes": True, "stderr_bytes": 0},
                      {"stdout_bytes": -1, "stderr_bytes": 0},
                      {"stdout_bytes": 1},
                      {"stdout_bytes": 1, "stderr_bytes": 0, "unknown": 1}):
            with self.subTest(extra=extra):
                self.progress_snapshot = {"stdout": "", "stderr": "", "stdout_truncated": False,
                                          "stderr_truncated": False, **extra}
                updates = []
                with self.assertRaisesRegex(ValueError, "invalid task progress"):
                    await self.client.execute("true", str(self.root), 1, asyncio.Event(),
                                              job_id="a" * 32, on_output=updates.append)
                self.assertEqual(updates, [])

    async def test_progress_on_existing_socket_precedes_final_result(self):
        snapshot = {"stdout": "hello", "stderr": "warning", "stdout_truncated": False,
                    "stderr_truncated": False}
        self.progress_snapshot = snapshot
        updates = []
        received = asyncio.Event()
        def on_output(value):
            updates.append(value)
            received.set()
        task = asyncio.create_task(self.client.execute(
            "true", str(self.root), 1, asyncio.Event(), job_id="a" * 32,
            on_output=on_output))
        self.tasks.append(task)
        await asyncio.wait_for(received.wait(), 2)
        self.assertEqual(updates, [snapshot])
        self.assertFalse(task.done())
        self.release_result.set()
        self.cleaned.set()
        self.assertEqual((await task)["stdout"], "ok")

    async def test_dropped_greeting_is_not_admitted_without_retry(self):
        # A full socket unit (MaxConnections) closes the connection before any
        # greeting. No command was sent: report "not admitted", do not retry.
        attempts = 0

        async def connect():
            nonlocal attempts
            attempts += 1
            local, remote = socket.socketpair()
            remote.close()
            return await asyncio.open_connection(sock=local)

        self.client._connect = connect
        with self.assertRaises(Refusal) as caught:
            await self.launch()
        self.assertEqual(caught.exception.as_data(), {
            "code": "not_admitted",
            "message": "task runner closed the connection before dispatch; no command was started"})
        self.assertEqual(attempts, 1)
        self.assertEqual(self.requests, [])

    async def test_a_silent_task_runner_is_reported_as_such(self):
        # A runner that starts too slowly is not one that closed the connection.
        kept = []

        async def connect():
            local, remote = socket.socketpair()
            kept.append(remote)
            return await asyncio.open_connection(sock=local)

        self.client._connect = connect
        with patch.object(task_client, "GREETING_SECONDS", 0.05), self.assertRaises(Refusal) as caught:
            await self.launch()
        kept[0].close()
        self.assertEqual(caught.exception.as_data(), {
            "code": "not_admitted",
            "message": "task runner did not answer within 0.05 seconds; no command was started"})
        self.assertEqual(self.requests, [])

    async def test_result_waits_for_cgroup_cleanup(self):
        task=self.launch()
        await self.sent.wait()
        await asyncio.sleep(0.03)
        self.assertFalse(task.done())
        self.cleaned.set()
        result=await task
        self.assertEqual(result['stdout'],'ok')
        # A returned result means cleanup was confirmed; counters go to watch.
        self.assertFalse({'cleanup','resources','command','environment'} & set(result))

    async def test_observation_never_reports_cleanup_before_confirmation(self):
        events=[]
        class Sink:
            def emit(self, event, **fields):
                events.append((event, fields))
            def close(self):
                pass
        with patch("boring_worker_kit.task_client.Observation", return_value=Sink()):
            task=self.launch()
            await self.sent.wait()
            await asyncio.sleep(.03)
            self.assertEqual([e[0] for e in events], ["dispatch"])
            self.cleaned.set()
            await task
        name, fields = events[-1]
        resources = fields.pop("resources")
        self.assertEqual((name, fields), ("cleanup", dict(complete=True, cancelled=False,
                                                         result_received=True, failed=False)))
        self.assertEqual(set(resources), {"task_before_cleanup", "shared_events_scope",
                                          "shared_event_delta"})

    async def test_observation_reports_uncertain_cleanup(self):
        events=[]
        class Sink:
            def emit(self, event, **fields):
                events.append((event,fields))
            def close(self):
                pass
        self.errors.append(RuntimeError("not empty"))
        self.cleaned.set()
        with patch("boring_worker_kit.task_client.Observation", return_value=Sink()):
            with self.assertRaises(ContainmentError):
                await self.launch()
        self.assertEqual(events[-1][0], "cleanup")
        self.assertIs(events[-1][1]["complete"], False)
        self.assertIn("task_before_cleanup", events[-1][1]["resources"])

    async def test_cancel_waits_for_cleanup(self):
        self.hold_response=True
        stop=asyncio.Event()
        task=self.launch(stop)
        await self.sent.wait()
        stop.set()
        await asyncio.sleep(0.03)
        self.assertFalse(task.done())
        # The connection is dropped at cancellation, before cleanup is confirmed.
        await asyncio.wait_for(self.client_eof.wait(), 1)
        self.assertFalse(task.done())
        self.cleaned.set()
        self.assertEqual(await task,{'cancelled':True})

    async def test_cancel_before_handshake_sends_no_command(self):
        stop=asyncio.Event(); stop.set(); self.cleaned.set()
        self.assertEqual(await self.launch(stop),{'cancelled':True})
        self.assertEqual(self.requests,[])

    async def test_cleanup_failure_disables_environment(self):
        executor=EnvironmentExecutor(self.settings, HostLimits(1, 290))
        executor._tasks=self.client
        self.errors.append(RuntimeError('not empty'))
        self.cleaned.set()
        with self.assertRaises(ContainmentError):
            await executor.execute('true',None,None)
        with self.assertRaisesRegex(ContainmentError,'admission disabled'):
            await executor.execute('true',None,None)
        self.assertEqual(len(self.requests),1)

    async def test_late_cleanup_restores_admission_without_replay(self):
        # Real framed sockets with a controlled cleanup observer.
        executor=EnvironmentExecutor(self.settings, HostLimits(1, 290))
        executor._tasks=self.client
        self.errors.append(RuntimeError('not empty'))
        self.cleaned.set()
        with self.assertRaises(ContainmentError):
            await executor.execute('true',None,None)
        self.errors.clear()
        self.assertEqual((await executor.execute('next',None,None))['exit_code'], 0)
        self.assertEqual([request['command'] for request in self.requests], ['true', 'next'])

    async def test_cleanup_diagnostic_has_identity_and_phase_without_secrets(self):
        secret='DO_NOT_LOG_TEST_SECRET'
        self.errors.append(RuntimeError(secret))
        self.cleaned.set()
        with self.assertLogs('boring_worker_kit.task_client',level='ERROR') as logs:
            with self.assertRaises(ContainmentError) as caught:
                await self.client.execute('printf '+secret,str(self.root),1,asyncio.Event())
        message=' '.join(logs.output)
        self.assertIn('unit=task-group',message)
        self.assertIn('result_received=True',message)
        self.assertIn('cancelled=False',message)
        self.assertIn('execution_error_type=none',message)
        self.assertIn('cleanup_error_type=RuntimeError',message)
        self.assertIn('elapsed_seconds=',message)
        self.assertNotIn(secret,message)
        self.assertIs(caught.exception.__cause__,self.errors[0])

    async def test_diagnostic_failure_does_not_replace_containment_error(self):
        self.errors.append(RuntimeError('not empty'))
        self.cleaned.set()
        with patch('boring_worker_kit.task_client.logger.error',side_effect=OSError('journal unavailable')):
            with self.assertRaises(ContainmentError) as caught:
                await self.launch()
        self.assertIs(caught.exception.__cause__,self.errors[0])

    async def test_watch_failure_cannot_lose_unresolved_group(self):
        self.errors.append(RuntimeError('not empty'))
        self.cleaned.set()
        class Sink:
            def emit(self, event, **fields):
                if event == "cleanup":
                    raise OSError("watch unavailable")
            def close(self):
                pass
        with patch("boring_worker_kit.task_client.Observation", return_value=Sink()):
            with self.assertRaises(ContainmentError) as caught:
                await self.launch()
        self.assertIsNotNone(caught.exception.group)
        self.assertIs(caught.exception.__cause__, self.errors[0])

    async def test_bad_protocol_never_sends_command(self):
        self.hello['version']=99
        with self.assertRaisesRegex(ValueError,'protocol'):
            await self.launch()
        self.assertEqual(self.requests,[])

    async def test_slot_held_through_cleanup_and_repeated_cancellation(self):
        executor = EnvironmentExecutor(self.settings, HostLimits(1, 290))
        executor._tasks = self.client
        self.hold_response = True
        first = asyncio.create_task(executor.execute('first', None, None))
        self.tasks.append(first)
        await self.sent.wait()
        second = asyncio.create_task(executor.execute('second', None, None))
        self.tasks.append(second)
        first.cancel()
        await asyncio.sleep(0.02)
        first.cancel()
        await asyncio.sleep(0.02)
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(first.done())
        self.hold_response = False
        self.cleaned.set()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual((await second)['stdout'], 'ok')
        self.assertEqual(len(self.requests), 2)

    async def test_rejects_worker_writable_socket_ancestry(self):
        # Real /tmp is writable even when the leaf endpoint itself is a socket.
        client = TaskClient(self.settings)
        with self.assertRaisesRegex(ValueError, 'ancestry'):
            await client._connect()

    async def test_failure_reports_shared_limit_evidence_to_watch(self):
        (self.root/'memory.events').write_text('max 1\noom_kill 0\n')
        self.response_error = True
        self.cleaned.set()
        events = []
        class Sink:
            def emit(self, event, **fields):
                events.append((event, fields))
            def close(self):
                pass
        with patch("boring_worker_kit.task_client.Observation", return_value=Sink()):
            with self.assertRaisesRegex(RuntimeError, '^runner failure$'):
                await self.launch()
        resources = events[-1][1]['resources']
        self.assertEqual(resources['shared_event_delta']['memory.events'],
                         {'max': 3, 'oom_kill': 1})
        self.assertIn('overlapping', resources['shared_events_scope'])


class CgroupTest(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_empty_or_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            events=root/'cgroup.events'
            events.write_text('populated 1\n')
            task=asyncio.create_task(TaskCgroup(root).wait_empty(0.5))
            await asyncio.sleep(0.03)
            self.assertFalse(task.done())
            events.write_text('populated 0\n')
            await task
            events.unlink()
            await TaskCgroup(root).wait_empty()

    async def test_removal_read_race_and_unrelated_io_error(self):
        group=TaskCgroup(Path('/unread'))
        with patch.object(Path, 'read_text', side_effect=OSError(errno.ENODEV, 'removed')):
            await group.wait_empty()
        with patch.object(Path, 'read_text', side_effect=OSError(errno.EIO, 'broken')):
            with self.assertRaises(OSError) as caught:
                await group.wait_empty()
        self.assertEqual(caught.exception.errno, errno.EIO)

    async def test_population_timeout_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/'cgroup.events').write_text('populated 1\n')
            with self.assertRaises(ContainmentError):
                await TaskCgroup(root).wait_empty(0.03)

    async def test_rejects_unrelated_or_traversal_paths(self):
        for value in ('/system.slice/other.service','/system.slice/../x/task-test@1.service','/task-test@1.service'):
            with self.assertRaises(ValueError):
                TaskCgroup.observe({'cgroup':value,'pid':os.getpid()},'task-test',
                                   Path('/system.slice/system-crocodilepond.slice'))

class ResourceCounterTest(unittest.TestCase):
    def test_available_counters_and_missing_peak_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'cpu.stat').write_text('usage_usec 123\nuser_usec 100\nsystem_usec 23\n')
            (root/'memory.current').write_text('4096\n')
            (root/'memory.events').write_text('max 2\noom_kill 1\n')
            result = TaskCgroup(root).snapshot()
            self.assertEqual(result['counters']['cpu.stat']['usage_usec'], 123)
            self.assertEqual(result['counters']['memory.current'], 4096)
            self.assertIn('memory.peak', result['unavailable'])
            self.assertNotIn('memory.peak', result['counters'])

    def test_shared_event_delta_excludes_prior_events(self):
        before = {'counters': {'memory.events': {'max': 7, 'oom_kill': 2}}}
        after = {'counters': {'memory.events': {'max': 10, 'oom_kill': 3}}}
        result = TaskCgroup.event_delta(before, after)
        self.assertEqual(result['memory.events'], {'max': 3, 'oom_kill': 1})
        self.assertIsNone(result['memory.swap.events'])
        self.assertNotIn('pids.events', result)

    def test_counter_removal_is_unavailable_not_zero(self):
        result = TaskCgroup.event_delta({'counters': {'memory.events': {'max': 7}}},
                                        {'counters': {}})
        self.assertIsNone(result['memory.events'])
