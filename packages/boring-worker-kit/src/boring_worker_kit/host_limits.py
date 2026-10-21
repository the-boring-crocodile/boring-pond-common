"""Read the fixed Pond units' effective limits from systemd; never change them."""
from __future__ import annotations

from dataclasses import dataclass
import re
import subprocess
import time

from .inputs import MAX_JOB_SECONDS
from .settings import ExecutionSettings as EnvironmentSettings
from .settings import valid_unit_prefix

USEC_INFINITY = 2**64 - 1
TASK_OVERHEAD_SECONDS = 10
HOST_INSPECTION_SECONDS = 3
FOREGROUND_REQUEST_SECONDS = 103
_UNIT = re.compile(r"[A-Za-z0-9_-]+\Z")


@dataclass(frozen=True)
class HostLimits:
    task_slots: int
    job_timeout_seconds: float | None  # None means systemd has no runtime cap.

    @property
    def job_max_seconds(self) -> float:
        """The longest a command job may run here."""
        if self.job_timeout_seconds is None:
            return MAX_JOB_SECONDS
        return min(MAX_JOB_SECONDS, self.job_timeout_seconds)

    @property
    def exec_timeout_seconds(self) -> float:
        return min(float(FOREGROUND_REQUEST_SECONDS), self.job_timeout_seconds) if self.job_timeout_seconds is not None else float(FOREGROUND_REQUEST_SECONDS)

    def public(self) -> dict[str, object]:
        return {"exec_timeout_seconds": self.exec_timeout_seconds,
                "job_timeout_seconds": self.job_max_seconds,
                "task_slots": self.task_slots}


def _run(args: list[str], *, timeout: float) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=True, timeout=timeout)
    return result.stdout.strip()


def read_host_limits(settings: EnvironmentSettings) -> HostLimits:
    """Inspect a loaded socket and an inactive template instance, without starting it."""
    deadline = time.monotonic() + HOST_INSPECTION_SECONDS

    def inspect(args):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("host limit inspection exceeded its startup budget")
        result = _run(args, timeout=remaining)
        if time.monotonic() > deadline:
            raise TimeoutError("host limit inspection exceeded its startup budget")
        return result

    prefix = settings.task_unit_prefix
    if (not _UNIT.fullmatch(prefix) or
            not valid_unit_prefix(prefix, settings.name)):
        raise ValueError("invalid fixed task unit prefix")
    socket = prefix + ".socket"
    template = prefix + "@limits-inspection.service"
    slots = int(inspect(["/usr/bin/systemctl", "show", "--no-pager", "-P",
                      "MaxConnections", socket]))
    if slots < 1:
        raise ValueError("invalid effective task socket capacity")
    path = inspect(["/usr/bin/busctl", "call", "org.freedesktop.systemd1",
                 "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager",
                 "LoadUnit", "s", template])
    match = re.fullmatch(r'o "(/org/freedesktop/systemd1/unit/[A-Za-z0-9_]+)"', path)
    if match is None:
        raise ValueError("cannot resolve task service template")
    value = inspect(["/usr/bin/busctl", "get-property", "org.freedesktop.systemd1",
                  match[1], "org.freedesktop.systemd1.Service", "RuntimeMaxUSec"])
    match = re.fullmatch(r"t ([0-9]+)", value)
    if match is None:
        raise ValueError("cannot read task service lifetime")
    micros = int(match[1])
    if micros == USEC_INFINITY:
        maximum = None
    else:
        maximum = micros / 1_000_000 - TASK_OVERHEAD_SECONDS
        if maximum < 0.1:
            raise ValueError("task service lifetime leaves no command/cleanup time")
    return HostLimits(slots, maximum)
