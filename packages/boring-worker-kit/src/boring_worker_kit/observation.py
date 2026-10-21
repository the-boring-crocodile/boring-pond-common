"""Best-effort, bounded observation; never part of command ownership."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import socket
import stat
import time
import uuid
from typing import Any
from .settings import ExecutionSettings as EnvironmentSettings

MAX_EVENT_BYTES = 8192
CHUNK_BYTES = 2048


def watch_path(settings: EnvironmentSettings) -> Path | None:
    """The environment's admin watch socket, or None when it has no watch root."""
    root = getattr(settings, "watch_root", None)
    return None if root is None else Path(root) / (settings.task_unit_prefix + ".sock")


def protected_directory(path: Path) -> None:
    """No symlink or non-root writer may replace the observation destination."""
    for directory in (path, *path.parents):
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o022):
            raise ValueError("watch directory ancestry must be root-owned and not group/world writable")


class Observation:
    """One nonblocking datagram sender per lifecycle owner; no retry queue."""

    def __init__(self, settings: Any, task: str, source: str, job_id: str | None = None) -> None:
        self.sock = None
        self.sequence = 0
        self.dropped = 0
        self.identity = dict(version=1, task=task, source=source,
                             sender=uuid.uuid4().hex, environment=settings.name)
        if job_id is not None:
            self.identity["job_id"] = job_id
        candidate = None
        try:
            path = watch_path(settings)
            if path is None:
                return
            protected_directory(path.parent)
            info = path.lstat()
            if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0
                    or stat.S_IMODE(info.st_mode) != 0o622):
                return
            candidate = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            candidate.setblocking(False)
            candidate.connect(str(path))
            self.sock = candidate
        except (OSError, ValueError):
            if candidate is not None:
                candidate.close()

    def emit(self, event: str, **fields: Any) -> None:
        if self.sock is None:
            return
        self.sequence += 1
        try:
            payload = json.dumps(dict(self.identity, event=event,
                sequence=self.sequence, dropped=self.dropped,
                monotonic=time.monotonic(), **fields), ensure_ascii=True).encode()
            if len(payload) > MAX_EVENT_BYTES:
                self.dropped += 1
                return
            self.sock.send(payload)
        except (BlockingIOError, InterruptedError):
            self.dropped += 1
        except (OSError, TypeError, ValueError):
            self.dropped += 1
            self.close()

    def output(self, fd, data):
        if self.sock is None:
            return
        for offset in range(0, len(data), CHUNK_BYTES):
            self.emit("output", stream="stdout" if fd == 1 else "stderr",
                      data=base64.b64encode(data[offset:offset + CHUNK_BYTES]).decode())

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None
