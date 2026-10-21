"""Async process lifetime and bounded output, with process-group cleanup.

Process groups do not contain descendants that create new sessions. Kernel-backed
per-task containment remains a deployment gate for the stronger lifetime contract.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from typing import Callable

# How long the development path waits, after SIGKILL, to see the process group
# gone. Bounded, but generous: on a loaded host the kernel can take more than a
# fraction of a second to tear down killed processes.
GROUP_EXIT_SECONDS = 5.0


class BoundedOutput:
    """Bounded head and rolling tail; count bytes without retaining the middle."""

    def __init__(self, limit: int):
        self.limit = limit
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def append(self, data: bytes) -> None:
        self.total += len(data)
        head_limit = (self.limit + 1) // 2
        take = min(len(data), max(0, head_limit - len(self.head)))
        self.head.extend(data[:take])
        tail_limit = self.limit - head_limit
        if tail_limit:
            self.tail.extend(data[max(take, len(data) - tail_limit):])
            del self.tail[:-tail_limit]

    def value(self) -> str:
        if self.total <= self.limit:
            return (self.head + self.tail).decode(errors="replace")
        marker = f"\n[... output truncated; {self.total} bytes total ...]\n".encode()
        if len(marker) > self.limit:
            marker = b"[...]"[:self.limit]
        available = self.limit - len(marker)
        first = (available + 1) // 2
        last = available - first
        # Decode parts independently: cut code points must not join across the omitted span.
        return (self.head[:first].decode(errors="replace") + marker.decode()
                + (self.tail[-last:].decode(errors="replace") if last else ""))


class ProcessOutput(asyncio.SubprocessProtocol):
    """Keep exit notification independent of pipes inherited by descendants."""

    def __init__(self, limit: int, observer=None,
                 on_output: Callable[[dict[str, object]], None] | None = None) -> None:
        self.observer = observer
        self.on_output = on_output
        self.last_snapshot = 0.0
        self.pending_snapshot: asyncio.TimerHandle | None = None
        loop = asyncio.get_running_loop()
        self.exited = loop.create_future()
        self.closed = loop.create_future()
        self.limit = limit
        self.buffers = {1: BoundedOutput(limit), 2: BoundedOutput(limit)}
        self.truncated = {1: False, 2: False}
        self.open_pipes = {1, 2}

    def pipe_data_received(self, fd: int, data: bytes) -> None:
        if fd not in self.buffers:
            return
        target = self.buffers[fd]
        remaining = max(0, self.limit - target.total)
        target.append(data)
        was_truncated = self.truncated[fd]
        self.truncated[fd] |= len(data) > remaining
        if self.on_output is not None:
            now = time.monotonic()
            if now - self.last_snapshot >= .1:
                self.publish_snapshot()
            elif self.pending_snapshot is None:
                self.pending_snapshot = asyncio.get_running_loop().call_later(
                    max(0, self.last_snapshot + .1 - now), self.publish_snapshot)
        if self.observer is not None:
            # Observation failures must never interrupt pipe draining or cleanup.
            try:
                if remaining:
                    self.observer.output(fd, data[:remaining])
                if self.truncated[fd] and not was_truncated:
                    self.observer.emit("output_truncated", stream="stdout" if fd == 1 else "stderr")
            except Exception:
                pass

    def cancel_pending_snapshot(self) -> None:
        if self.pending_snapshot is not None:
            self.pending_snapshot.cancel()
            self.pending_snapshot = None

    def publish_snapshot(self) -> None:
        self.cancel_pending_snapshot()
        self.last_snapshot = time.monotonic()
        if self.on_output is not None:
            self.on_output(self.snapshot())

    def snapshot(self) -> dict[str, object]:
        return {
            "stdout": self.buffers[1].value(),
            "stderr": self.buffers[2].value(),
            "stdout_truncated": self.truncated[1],
            "stderr_truncated": self.truncated[2],
            "stdout_bytes": self.buffers[1].total,
            "stderr_bytes": self.buffers[2].total,
        }

    def pipe_connection_lost(self, fd: int, exc: Exception | None) -> None:
        self.open_pipes.discard(fd)

    def process_exited(self) -> None:
        if not self.exited.done():
            self.exited.set_result(None)

    def connection_lost(self, exc: Exception | None) -> None:
        if not self.closed.done():
            self.closed.set_result(None)


def signal_group(pid: int, sig: signal.Signals) -> bool:
    try:
        os.killpg(pid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        # PermissionError: the group is gone and its ID now belongs to another
        # identity's process; there is nothing of ours left to signal.
        return False


class ProcessCleanupError(RuntimeError):
    """The development process group has not been observed to finish."""


def group_running(group: int) -> bool:
    """Observe group members; exited zombies need reaping, not further killing."""
    with os.scandir("/proc") as entries:
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            try:
                with open(entry.path + "/stat", "rb") as handle:
                    fields = handle.read(4096).rsplit(b")", 1)[1].split()
            except (FileNotFoundError, ProcessLookupError):
                continue
            if int(fields[2]) == group and fields[0] not in (b"Z", b"X"):
                return True
    return False


async def wait_group_exit(group: int) -> None:
    # Only the explicit development path uses this observation. The deployed
    # task runner relies on systemd/cgroup cleanup, including escaped sessions.
    deadline = asyncio.get_running_loop().time() + GROUP_EXIT_SECONDS
    try:
        while group_running(group):
            if asyncio.get_running_loop().time() >= deadline:
                raise ProcessCleanupError("process-group exit was not confirmed")
            await asyncio.sleep(.01)
    except (OSError, ValueError, IndexError) as error:
        raise ProcessCleanupError("process-group exit could not be observed") from error


async def stop_group(pid: int, output: ProcessOutput) -> None:
    # Leader exit does not establish descendant exit. Escalate for the group.
    if signal_group(pid, signal.SIGTERM):
        await asyncio.sleep(0.2)
        signal_group(pid, signal.SIGKILL)
    await output.exited


async def run_process(command: str, cwd: str, timeout: float,
                      output_limit: int, stop: asyncio.Event,
                      *, systemd_contained: bool = False, observer=None,
                      on_output: Callable[[dict[str, object]], None] | None = None) -> dict[str, object]:
    started = time.monotonic()
    output = ProcessOutput(output_limit, observer, on_output)
    result: dict[str, object] = {
        'cwd': cwd, 'exit_code': None,
        'stdout': '', 'stderr': '', 'timed_out': False,
        'stdout_truncated': False, 'stderr_truncated': False,
    }
    try:
        transport, _ = await asyncio.get_running_loop().subprocess_exec(
            lambda: output, '/bin/bash', '-lc', command, cwd=cwd,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True,
        )
    except OSError as error:
        # Not a command outcome: nothing ran, so there is no exit status to report.
        raise RuntimeError(f"command could not be started: {error}") from error

    try:
        if observer is not None:
            try:
                observer.emit("started", pid=transport.get_pid())
            except Exception:
                pass
        stopped = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait(
                (output.exited, stopped),
                timeout=max(0, timeout - (time.monotonic() - started)),
                return_when=asyncio.FIRST_COMPLETED,
            )
            result['timed_out'] = not done
        finally:
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)
            try:
                await stop_group(transport.get_pid(), output)
                if not systemd_contained:
                    await wait_group_exit(transport.get_pid())
                # A setsid descendant may retain a pipe despite group cleanup.
                try:
                    await asyncio.wait_for(asyncio.shield(output.closed), 0.5)
                except asyncio.TimeoutError:
                    if not systemd_contained:
                        raise RuntimeError('output pipes survived process-group cleanup; containment incomplete')
                    # systemd ends escaped descendants after this runner exits.
                    # Report bounded partial output, never wait indefinitely on their pipes.
                    result['output_incomplete'] = bool(output.open_pipes)
            finally:
                transport.close()

        if on_output is not None:
            output.publish_snapshot()
        result.update(
            exit_code=transport.get_returncode(),
            duration_seconds=round(time.monotonic() - started, 3),
        )
        result.update(output.snapshot())
        return result
    except BaseException:
        output.cancel_pending_snapshot()
        raise
