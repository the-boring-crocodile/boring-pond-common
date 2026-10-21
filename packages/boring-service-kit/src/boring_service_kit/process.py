"""Processes whose whole tree dies with them.

Windows: the shell starts suspended, joins a Job Object with
KILL_ON_JOB_CLOSE, then resumes, so no child escapes the job (the MCP SDK's
own helper assigns after start and documents that gap). POSIX (tests, Linux):
a new session, killed by process group.

Standalone process containment shared by services and core; no server imports.
Windows consumers require pywin32 (the MCP Windows dependency).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import IO

IS_WINDOWS = sys.platform == "win32"

# Windows process creation flags. subprocess exports CREATE_NO_WINDOW and
# CREATE_NEW_PROCESS_GROUP but not CREATE_SUSPENDED (an AttributeError on
# Windows), so all three are defined here from the Win32 API.
CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

if IS_WINDOWS:  # pragma: no cover - exercised on the Windows CI runner and the VM
    import ctypes

    import win32api
    import win32job

    _ntdll = ctypes.WinDLL("ntdll")
    _ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
    _ntdll.NtResumeProcess.restype = ctypes.c_long


class Tree:
    """A started process and everything it spawns."""

    def __init__(self, argv: list[str] | str, cwd: Path, env: dict[str, str],
                 stdout: IO[bytes] | int | None, stderr: IO[bytes] | int | None,
                 *, stdin: IO[bytes] | int | None = subprocess.DEVNULL) -> None:
        self._job = None
        self._closed = False
        if IS_WINDOWS:  # pragma: no cover
            self._job = _new_kill_on_close_job()
            flags = CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
            process = None
            try:
                process = self.popen = subprocess.Popen(
                    argv, cwd=cwd, env=env, stdin=stdin,
                    stdout=stdout, stderr=stderr, creationflags=flags)
                handle = int(self.popen._handle)  # type: ignore[attr-defined]
                win32job.AssignProcessToJobObject(self._job, handle)
                status = _ntdll.NtResumeProcess(handle)
                if status != 0:
                    raise OSError(f"NtResumeProcess failed with status {status:#x}")
            except BaseException:
                try:
                    if process is not None:
                        # Assignment may have failed: the suspended process is
                        # ours, but not necessarily a member of our Job Object.
                        process.kill()
                        process.wait(10)
                finally:
                    self._release_job()
                raise
        else:
            self.popen = subprocess.Popen(argv, cwd=cwd, env=env, stdin=stdin,
                                          stdout=stdout, stderr=stderr, start_new_session=True)

    @property
    def pid(self) -> int:
        return self.popen.pid

    def wait(self, timeout: float | None) -> int | None:
        """The exit code, or None if still running after `timeout` seconds."""
        try:
            return self.popen.wait(timeout)
        except subprocess.TimeoutExpired:
            return None

    def kill(self) -> None:
        """Request whole-tree termination and wait; failures are never settlement."""
        if self._closed:
            return
        if IS_WINDOWS:  # pragma: no cover - native acceptance plus injected boundaries
            if self._job is not None:
                win32job.TerminateJobObject(self._job, 1)
        else:
            try:
                os.killpg(self.popen.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.popen.wait(10)
        if IS_WINDOWS and self._job is not None:
            deadline = time.monotonic() + 10
            while True:
                info = win32job.QueryInformationJobObject(
                    self._job, win32job.JobObjectBasicAccountingInformation)
                active = info["ActiveProcesses"]
                if type(active) is not int or active < 0:
                    raise OSError("invalid Job Object active-process count; cleanup unconfirmed")
                if active == 0:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("Job Object still has active processes; cleanup unconfirmed")
                time.sleep(0.01)

    def _release_job(self) -> None:
        if IS_WINDOWS and self._job is not None:
            win32api.CloseHandle(self._job)
            self._job = None

    def close(self) -> None:
        """Settle the owned tree before releasing its containment handle."""
        if self._closed:
            return
        if IS_WINDOWS:
            if self._job is not None:
                self.kill()
                self._release_job()
        else:
            # POSIX is the portable test adapter; Windows owns the product contract.
            self.kill()
        self._closed = True


def _new_kill_on_close_job():  # pragma: no cover - Windows only
    job = win32job.CreateJobObject(None, "")
    try:
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        return job
    except BaseException:
        win32api.CloseHandle(job)
        raise


@dataclass(frozen=True)
class Settlement:
    state: str
    exit_code: int | None = None
    error: str | None = None
    cleanup_error: str | None = None


def settle(tree: Tree, stop_requested: threading.Event,
           timeout: float | None = None) -> Settlement:
    """One watcher owns wait and cleanup. Only confirmed cleanup can succeed.

    Callers own admission, metadata and publishing; no command is replayed.
    Diagnostics contain exception types and phases, never arbitrary output.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    state, code, error, cleanup_error = "failed", None, None, None
    try:
        while True:
            if stop_requested.is_set():
                state = "stopped"
                break
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                state = "timed_out"
                break
            code = tree.wait(0.1 if remaining is None else min(0.1, remaining))
            if code is not None:
                state = "exited"
                break
    except Exception as exc:
        error = f"{type(exc).__name__}: execution wait failed"
    finally:
        try:
            tree.close()
        except Exception as exc:
            state = "failed"
            cleanup_error = f"{type(exc).__name__}: cleanup unconfirmed"
    return Settlement(state, code if state == "exited" else None, error, cleanup_error)
