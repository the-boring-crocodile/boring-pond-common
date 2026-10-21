"""Connect to fixed systemd task services and retain ownership through cleanup."""
from __future__ import annotations

import asyncio
import errno
import os
import logging
from pathlib import Path
import stat
from typing import Callable

from .framing import read_frame, write_frame
from .settings import ExecutionSettings as EnvironmentSettings
from .observation import Observation
from .host_limits import HOST_INSPECTION_SECONDS, TASK_OVERHEAD_SECONDS
from .outcomes import Refusal


class ContainmentError(RuntimeError):
    """Cleanup is unconfirmed; group ownership transfers to the executor.

    Without a validated group, admission cannot safely recover automatically.
    """

    def __init__(self, message: str, *, group: TaskCgroup | None = None) -> None:
        super().__init__(message)
        self.group = group


class TaskCgroup:
    def __init__(self, path: Path, *, directory_fd: int | None = None) -> None:
        self.path = path
        self._directory_fd = directory_fd
        self._closed = False

    def close(self) -> None:
        """Release the retained identity after cleanup is established."""
        if self._directory_fd is not None:
            os.close(self._directory_fd)
            self._directory_fd = None
        self._closed = True

    def __del__(self) -> None:
        # Exceptions may be consumed outside EnvironmentExecutor.
        self.close()

    def is_empty(self) -> bool:
        """Observe the original group once; unreadable/malformed state raises.

        Production observers retain a directory descriptor. Reopening events
        relative to it cannot mistake a new group at the same path for this one.
        ENOENT/ENODEV mean the original kernfs node has been removed.
        """
        if self._closed:
            raise RuntimeError('task cgroup observer is closed')
        try:
            if self._directory_fd is None:
                raw = (self.path / 'cgroup.events').read_text()
            else:
                descriptor = os.open('cgroup.events', os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                                     dir_fd=self._directory_fd)
                with os.fdopen(descriptor) as stream:
                    raw = stream.read()
        except OSError as error:
            if error.errno in (errno.ENOENT, errno.ENODEV):
                return True
            raise
        events = dict(line.split() for line in raw.splitlines())
        if events.get('populated') not in ('0', '1'):
            raise ValueError('invalid task cgroup population')
        return events['populated'] == '0'

    @classmethod
    def observe(cls, hello: dict, prefix: str,
                parent: Path) -> TaskCgroup:
        raw = hello.get('cgroup')
        pid = hello.get('pid')
        if not isinstance(raw, str) or type(pid) is not int or pid <= 0:
            raise ValueError('invalid task greeting')
        path = Path(raw)
        if (path.parent != parent or '..' in path.parts
                or not path.name.startswith(prefix + '@')
                or not path.name.endswith('.service')):
            raise ValueError('unexpected task cgroup')
        proc = Path('/proc') / str(pid)
        if proc.stat().st_uid != os.getuid():
            raise ValueError('unexpected runner identity')
        if f'0::{raw}' not in (proc/'cgroup').read_text().splitlines():
            raise ValueError('runner/cgroup mismatch')
        group = Path('/sys/fs/cgroup') / raw.lstrip('/')
        descriptor = os.open(group, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        observer = cls(group, directory_fd=descriptor)
        try:
            if os.fstat(descriptor).st_uid != 0 or os.access(
                    'cgroup.procs', os.W_OK, dir_fd=descriptor):
                raise ValueError('task cgroup is not protected from the execution identity')
            if observer.is_empty():
                raise ValueError('runner cgroup must be populated before accepting work')
            return observer
        except BaseException:
            observer.close()
            raise

    def snapshot(self) -> dict:
        """Best-effort kernel counters; absent data is never reported as zero."""
        values = {}
        unavailable = []
        for name in ("cpu.stat", "memory.current", "memory.peak", "memory.swap.current",
                     "memory.events", "memory.swap.events", "pids.current", "pids.events"):
            try:
                raw = (self.path / name).read_text().strip()
                if name.endswith((".stat", ".events")):
                    values[name] = {k: int(v) for k, v in (line.split() for line in raw.splitlines())}
                else:
                    values[name] = int(raw)
            except (OSError, ValueError):
                unavailable.append(name)
        return {"counters": values, "unavailable": unavailable}

    @staticmethod
    def event_delta(before: dict, after: dict) -> dict:
        deltas = {}
        # Linux 5.15 pids.events counts at the requesting task cgroup, not
        # hierarchically at the budget parent. Keep it in the task snapshot.
        for name in ("memory.events", "memory.swap.events"):
            old = before["counters"].get(name)
            new = after["counters"].get(name)
            if old is None or new is None:
                deltas[name] = None
            else:
                deltas[name] = {k: v - old[k] for k, v in new.items()
                               if k in old and v >= old[k]}
        return deltas

    async def wait_empty(self, timeout: float = 6) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if self.is_empty():
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise ContainmentError('task cgroup did not empty; environment admission disabled')
            await asyncio.sleep(0.02)


logger = logging.getLogger(__name__)
GREETING_SECONDS = 5.0


class TaskClient:
    def __init__(self, settings: EnvironmentSettings) -> None:
        self.settings = settings

    async def _connect(self):
        path = self.settings.task_socket_path
        assert path is not None
        # A protected leaf directory is insufficient if an ancestor can be replaced.
        for directory in (path.parent, *path.parent.parents):
            if directory.is_symlink():
                raise ValueError('task socket directories must not be symlinks')
            if directory.stat().st_uid != 0 or os.access(directory, os.W_OK):
                raise ValueError('task socket ancestry must be root-owned and not writable by worker')
        if not stat.S_ISSOCK(path.stat().st_mode):
            raise ValueError('task endpoint is not a socket')
        return await asyncio.wait_for(asyncio.open_unix_connection(path), 5)

    async def _greet(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, dict]:
        # The socket unit closes a connection before any greeting when its
        # MaxConnections is full.
        # No command has been sent: that is "not admitted", reported as such
        # rather than retried here -- the caller decides whether to try again.
        reader, writer = await self._connect()
        try:
            hello = await asyncio.wait_for(read_frame(reader), GREETING_SECONDS)
            return reader, writer, hello
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, TimeoutError) as error:
            await self._close(writer)
            logger.info("task greeting failed before dispatch: %s", type(error).__name__)
            if isinstance(error, asyncio.IncompleteReadError):
                raise Refusal("not_admitted", "task runner closed the connection before dispatch; "
                                              "no command was started") from None
            raise Refusal("not_admitted", f"task runner did not answer within {GREETING_SECONDS:g} "
                                          "seconds; no command was started") from None
        except BaseException:
            await self._close(writer)
            raise

    @staticmethod
    async def _close(writer: asyncio.StreamWriter) -> None:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass

    async def _read_result(self, reader: asyncio.StreamReader, job_id: str | None,
                           on_output: Callable[[dict[str, object]], None] | None) -> dict:
        while True:
            message = await read_frame(reader)
            if message.get("type") != "progress":
                return message
            output = message.get("output")
            if (job_id is None or set(message) != {"type", "output"}
                    or not isinstance(output, dict)
                    or set(output) not in (
                        {"stdout", "stderr", "stdout_truncated", "stderr_truncated"},
                        {"stdout", "stderr", "stdout_truncated", "stderr_truncated", "stdout_bytes", "stderr_bytes"})
                    or any(type(output[key]) is not int or not 0 <= output[key] <= 2**63 - 1
                           for key in ("stdout_bytes", "stderr_bytes") if key in output)
                    or any(not isinstance(output[key], str) or len(output[key].encode()) > self.settings.output_limit_bytes * 3
                           for key in ("stdout", "stderr"))
                    or any(type(output[key]) is not bool for key in ("stdout_truncated", "stderr_truncated"))):
                raise ValueError("invalid task progress frame")
            if on_output is not None:
                on_output(output)

    async def execute(self, command: str, cwd: str, timeout: float,
                      stop: asyncio.Event, *, job_id: str | None = None,
                      on_output: Callable[[dict[str, object]], None] | None = None) -> dict[str, object]:
        reader, writer, hello = await self._greet()
        group = None
        response = None
        abandoned = None
        result = None
        failure = None
        resources = None
        budget = None
        budget_before = None
        observation = None
        try:
            if type(hello.get('version')) is not int or hello['version'] != 1:
                raise ValueError('unsupported task protocol')
            group = TaskCgroup.observe(hello, self.settings.task_unit_prefix,
                                       self.settings.task_cgroup_parent)
            observation = Observation(self.settings, hello["cgroup"], "client", job_id=job_id)
            budget = TaskCgroup(group.path.parent)
            budget_before = budget.snapshot()
            if stop.is_set():
                return {'cancelled': True}
            observation.emit("dispatch", command=command[:1024], command_truncated=len(command) > 1024,
                             cwd=cwd[:1024], cwd_truncated=len(cwd) > 1024, timeout_seconds=timeout)
            await write_frame(writer, {'command': command, 'cwd': cwd, 'timeout_seconds': timeout, 'job_id': job_id})
            response = asyncio.create_task(self._read_result(reader, job_id, on_output))
            abandoned = asyncio.create_task(stop.wait())
            # Include bounded host inspection and result/cleanup allowance.
            done, _ = await asyncio.wait((response, abandoned), timeout=timeout + HOST_INSPECTION_SECONDS + TASK_OVERHEAD_SECONDS,
                                        return_when=asyncio.FIRST_COMPLETED)
            if abandoned in done:
                return {'cancelled': True}
            if response not in done:
                raise RuntimeError('task runner response deadline exceeded')
            message = await response
            if message.get('ok') is not True:
                raise RuntimeError(str(message.get('error', 'task runner failed')))
            result = message.get('result')
            if not isinstance(result, dict):
                raise ValueError('invalid task result')
        except Exception as error:
            failure = error
        finally:
            if group is not None:
                resources = {"task_before_cleanup": group.snapshot(),
                             "shared_events_scope": "all commands overlapping this request"}
            writer.close()
            for task in (response, abandoned):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(t for t in (response, abandoned) if t is not None),
                                 return_exceptions=True)
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            if group is not None:
                cleanup_started = asyncio.get_running_loop().time()
                try:
                    await group.wait_empty()
                except Exception as error:
                    # Persist identity/phase even when no watch viewer is attached.
                    # Never log command, cwd, output or exception message here.
                    try:
                        logger.error(
                            "task cleanup unconfirmed unit=%s elapsed_seconds=%.3f "
                            "result_received=%s cancelled=%s execution_error_type=%s "
                            "cleanup_error_type=%s",
                            group.path.name,
                            asyncio.get_running_loop().time() - cleanup_started,
                            result is not None, stop.is_set(),
                            type(failure).__name__ if failure is not None else "none",
                            type(error).__name__)
                    except Exception:
                        pass  # Diagnostics must never replace the containment failure.
                    try:
                        if observation is not None:
                            observation.emit("cleanup", complete=False, resources=resources)
                            observation.close()
                    finally:
                        # Watch diagnostics cannot lose the retained cleanup owner.
                        raise ContainmentError('task cleanup not confirmed; environment admission disabled',
                                               group=group) from error
                else:
                    group.close()
            if budget is not None:
                resources["shared_event_delta"] = TaskCgroup.event_delta(budget_before, budget.snapshot())
            # Resource counters are for admins (watch), not agents' results.
            if observation is not None:
                observation.emit("cleanup", complete=True, cancelled=stop.is_set(),
                                 result_received=result is not None, failed=failure is not None,
                                 resources=resources)
                observation.close()
        if failure is not None:
            raise failure
        assert result is not None
        return result
