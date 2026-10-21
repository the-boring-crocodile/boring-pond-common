"""The task runner's wire protocol as its client sees it, over a real Unix socket."""
import asyncio
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from boring_worker_kit import task_runner
from boring_worker_kit.framing import read_frame, write_frame
from boring_worker_kit.host_limits import HostLimits
from boring_worker_kit.settings import EnvironmentSettings

PARENT = "/system.slice/system-test.slice"
CGROUP = f"{PARENT}/runner-test@1.service"


class CgroupFile:
    def read_text(self) -> str:
        return f"0::{CGROUP}\n"


def fake_path(value):
    """The runner checks its own systemd cgroup; the test process has another one."""
    return CgroupFile() if str(value) == "/proc/self/cgroup" else Path(value)


class TaskRunnerProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_client_that_never_closes_gets_exactly_one_result_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = EnvironmentSettings(name="fixture", default_cwd=root, allowed_roots=(root,),
                                           task_socket_path=None, output_limit_bytes=1024,
                                           task_unit_prefix="runner-test", task_cgroup_parent=Path(PARENT))
            runner_end, client_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            saved_stdin = os.dup(0)
            os.dup2(runner_end.fileno(), 0)  # what systemd's socket activation hands the runner
            runner_end.close()
            try:
                with patch.object(task_runner, "Path", fake_path), \
                        patch.object(task_runner, "read_host_limits", return_value=HostLimits(1, None)), \
                        patch.object(task_runner, "RECEIPT_SECONDS", .3):
                    runner = asyncio.create_task(task_runner.serve(settings))
                    reader, writer = await asyncio.open_connection(sock=client_end)
                    greeting = await read_frame(reader)
                    self.assertEqual(greeting["cgroup"], CGROUP)
                    await write_frame(writer, {"command": "printf done", "timeout_seconds": 5})
                    answer = await asyncio.wait_for(read_frame(reader), 10)
                    self.assertTrue(answer["ok"], answer)
                    self.assertEqual(answer["result"]["stdout"], "done")
                    # The client stays silent past the receipt wait: the runner ends without
                    # another frame (before the fix: a second, error frame "TimeoutError").
                    await asyncio.wait_for(runner, 10)
                    self.assertEqual(await asyncio.wait_for(reader.read(), 5), b"")
                    writer.close()
                    await writer.wait_closed()
            finally:
                os.dup2(saved_stdin, 0)
                os.close(saved_stdin)
