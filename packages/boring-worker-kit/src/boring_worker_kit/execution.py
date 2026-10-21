from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from pathlib import Path

from .process import ProcessCleanupError, run_process
from .settings import ExecutionSettings as EnvironmentSettings
from .host_limits import HOST_INSPECTION_SECONDS, HostLimits, TASK_OVERHEAD_SECONDS
from .inputs import MAX_JOB_SECONDS
from .framing import MAX_COMMAND_BYTES
from .outcomes import Refusal
from .task_client import ContainmentError, TaskCgroup, TaskClient


# Runner-side time around a foreground command: host inspection, group stop, reply.
FOREGROUND_RESULT_SLACK_SECONDS = HOST_INSPECTION_SECONDS + 2.0

NOT_ADMITTED_MESSAGE = "foreground admission timeout; command not started"
INTERRUPTED_MESSAGE = ("foreground request deadline exceeded; task cleanup completed; "
                       "command may have started")


class ForegroundAdmissionTimeout(Refusal):
    """The command did not enter the executor before the deadline: not started."""

    def __init__(self, message: str = NOT_ADMITTED_MESSAGE) -> None:
        super().__init__("not_admitted", message)


class ForegroundInterrupted(Exception):
    """An admitted request exceeded its deadline after owned cleanup.

    A failure, not a refusal: the command may have started and had effects,
    and a refusal means the work did not run.
    """

    def __init__(self, message: str = INTERRUPTED_MESSAGE) -> None:
        super().__init__(message)
        self.message = message


def prepare_command(settings: EnvironmentSettings, command: str, cwd: str | None,
                    timeout_seconds: float | None, limits: HostLimits,
                    *, is_job: bool = False) -> tuple[str, float]:
    if not isinstance(command, str) or not command:
        raise Refusal("invalid_request", 'command must be a non-empty string')
    if len(command.encode("utf-8")) > MAX_COMMAND_BYTES:
        raise Refusal("limit", f'command exceeds {MAX_COMMAND_BYTES} UTF-8 bytes')
    requested = Path(cwd or settings.default_cwd)
    if not requested.is_absolute():
        requested = settings.default_cwd / requested
    effective_cwd = requested.resolve()
    if not any(effective_cwd == root or root in effective_cwd.parents
               for root in settings.allowed_roots):
        raise Refusal("invalid_request", 'working directory is outside allowed roots')
    if timeout_seconds is not None and (type(timeout_seconds) not in (int, float)
                                        or not math.isfinite(timeout_seconds)):
        raise Refusal("invalid_request", 'timeout must be a finite number')
    timeout = 120.0 if timeout_seconds is None else float(timeout_seconds)
    if timeout < 0.1:
        raise Refusal("invalid_request", 'timeout_seconds must be at least 0.1')
    if is_job and timeout > MAX_JOB_SECONDS:
        raise Refusal("invalid_request", f'a job timeout_seconds is at most {MAX_JOB_SECONDS:g}')
    return str(effective_cwd), min(timeout, limits.job_max_seconds if is_job
                                   else limits.exec_timeout_seconds)


class EnvironmentExecutor:
    """Validate and admit tasks; retain capacity until their owner finishes."""

    def __init__(self, settings: EnvironmentSettings, limits: HostLimits) -> None:
        self.settings = settings
        self.limits = limits
        self._slots = asyncio.Semaphore(limits.task_slots)
        self._unresolved: set[TaskCgroup] = set()
        self._containment_unknown = False
        self._tasks = TaskClient(settings) if settings.task_socket_path else None

    def _check_containment(self) -> None:
        """Recheck failed cleanup before admission, without replay or killing.

        No await between observation and admission: concurrent failures cannot
        be cleared by an older check. Groups with unknown state remain retained.
        """
        for group in tuple(self._unresolved):
            try:
                empty = group.is_empty()
            except (OSError, ValueError):
                continue
            if empty:
                group.close()
                self._unresolved.remove(group)
        if self._containment_unknown or self._unresolved:
            raise ContainmentError('environment admission disabled after unconfirmed cleanup')

    async def execute(self, command: str, cwd: str | None,
                      timeout_seconds: float | None,
                      on_admitted: Callable[[], None] | None = None,
                      *, job_id: str | None = None,
                      on_output: Callable[[dict[str, object]], None] | None = None,
                      foreground_deadline: float | None = None) -> dict[str, object]:
        if job_id is not None:
            return await self._execute(command, cwd, timeout_seconds, on_admitted,
                                       job_id=job_id, on_output=on_output)

        admitted = False

        def mark_admitted():
            nonlocal admitted
            admitted = True
            if on_admitted is not None:
                on_admitted()

        budget = self.limits.exec_timeout_seconds
        # Stop useful work early enough to retain ownership through cleanup.
        reserve = min(TASK_OVERHEAD_SECONDS, budget / 2)
        deadline = asyncio.get_running_loop().time() + budget - reserve
        if foreground_deadline is not None:
            remaining = foreground_deadline - asyncio.get_running_loop().time()
            deadline = min(deadline, foreground_deadline - min(TASK_OVERHEAD_SECONDS, max(remaining, 0) / 2))
        caller = asyncio.current_task()
        expired = False

        def expire():
            nonlocal expired
            expired = True
            caller.cancel()

        # Cancel this owner, rather than introducing a second cleanup owner.
        timer = asyncio.get_running_loop().call_at(deadline, expire)
        try:
            return await self._execute(command, cwd, timeout_seconds, mark_admitted, deadline=deadline)
        except asyncio.CancelledError as error:
            if not expired:
                raise
            if hasattr(caller, "uncancel"):
                caller.uncancel()
            if not admitted:
                raise ForegroundAdmissionTimeout() from error
            raise ForegroundInterrupted() from error
        finally:
            timer.cancel()

    async def _execute(self, command: str, cwd: str | None,
                       timeout_seconds: float | None,
                       on_admitted: Callable[[], None] | None = None,
                       *, job_id: str | None = None,
                       on_output: Callable[[dict[str, object]], None] | None = None,
                       deadline: float | None = None) -> dict[str, object]:
        effective_cwd, timeout = prepare_command(self.settings, command, cwd, timeout_seconds,
                                                 self.limits, is_job=job_id is not None)

        async with self._slots:
            self._check_containment()
            if deadline is not None:
                # The runner inspects host limits before starting and stops the
                # group after the timeout; leave it that time so a timed-out
                # command reports its result before the caller's deadline. A
                # budget too small for the full slack keeps half for the command,
                # so a timeout there can still end as an unknown outcome.
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise ForegroundAdmissionTimeout()
                timeout = min(timeout, remaining - min(FOREGROUND_RESULT_SLACK_SECONDS, remaining / 2))
            if on_admitted is not None:
                on_admitted()
            stop = asyncio.Event()
            if self._tasks is not None:
                operation = self._tasks.execute(command, effective_cwd, timeout, stop,
                                                job_id=job_id, on_output=on_output)
            else:
                # Explicit local development mode; worker main refuses this in production.
                operation = run_process(command, effective_cwd, timeout,
                                        self.settings.output_limit_bytes, stop,
                                        on_output=on_output)
            owner = asyncio.create_task(operation)
            cancelled = False
            while True:
                try:
                    result = await asyncio.shield(owner)
                    break
                except ProcessCleanupError as error:
                    self._containment_unknown = True
                    raise ContainmentError(str(error)) from error
                except ContainmentError as error:
                    if error.group is None:
                        self._containment_unknown = True
                    else:
                        self._unresolved.add(error.group)
                    raise
                except asyncio.CancelledError:
                    if owner.cancelled():
                        raise
                    cancelled = True
                    stop.set()
                    # Repeated caller cancellation must not interrupt cleanup.
            if cancelled:
                raise asyncio.CancelledError
        # The task runner reports its final host-checked value. The local
        # development path uses the value admitted by this executor.
        result.setdefault('effective_timeout_seconds', timeout)
        return result
