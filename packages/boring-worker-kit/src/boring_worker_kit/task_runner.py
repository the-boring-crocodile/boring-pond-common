"""One command per socket-activated, fixed-identity systemd service instance."""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import socket
import re

from .execution import prepare_command
from .host_limits import read_host_limits
from .framing import peer_uid, read_frame, write_frame
from .process import run_process
from .observation import Observation

# How long the runner keeps its cgroup after the result for the client's EOF.
RECEIPT_SECONDS = 5


async def serve(settings) -> None:
    accepted = socket.socket(fileno=os.dup(0))
    os.close(0)
    reader, writer = await asyncio.open_connection(sock=accepted)
    running = abandoned = progress_sender = None
    observation = None
    try:
        if peer_uid(writer) != os.getuid():
            raise PermissionError('task requester must have the configured execution identity')
        cgroup = next(line[3:] for line in Path('/proc/self/cgroup').read_text().splitlines()
                      if line.startswith('0::'))
        group = Path(cgroup)
        if (group.parent != settings.task_cgroup_parent
                or not group.name.startswith(settings.task_unit_prefix + '@')
                or not group.name.endswith('.service')):
            raise RuntimeError('task runner requires its configured systemd service cgroup')
        await write_frame(writer, {'version': 1, 'pid': os.getpid(), 'cgroup': cgroup})
        request = await asyncio.wait_for(read_frame(reader), 5)
        command = request.get('command')
        job_id = request.get('job_id')
        if job_id is not None and (not isinstance(job_id, str) or not re.fullmatch(r'[a-f0-9]{32}', job_id)):
            raise ValueError('invalid job identity')
        cwd, timeout = prepare_command(settings, command, request.get('cwd'),
                                       request.get('timeout_seconds'), read_host_limits(settings),
                                       is_job=job_id is not None)
        observation = Observation(settings, cgroup, "runner", job_id=job_id)
        observation.emit("accepted", command=command[:1024], command_truncated=len(command) > 1024,
                         cwd=cwd[:1024], cwd_truncated=len(cwd) > 1024, timeout_seconds=timeout)
        stop = asyncio.Event()
        latest: dict[str, object] | None = None
        changed = asyncio.Event()

        def capture(snapshot: dict[str, object]) -> None:
            nonlocal latest
            latest = snapshot  # Replace intermediate snapshots; never queue unbounded output.
            changed.set()

        async def send_progress() -> None:
            while True:
                await changed.wait()
                changed.clear()
                snapshot = latest
                if snapshot is not None:
                    await write_frame(writer, {"type": "progress", "output": snapshot})

        if job_id is not None:
            progress_sender = asyncio.create_task(send_progress())
        running = asyncio.create_task(run_process(command, cwd, timeout, settings.output_limit_bytes,
                                                  stop, systemd_contained=True, observer=observation,
                                                  on_output=capture if job_id is not None else None))
        abandoned = asyncio.create_task(reader.read(1))
        done, _ = await asyncio.wait((running, abandoned), return_when=asyncio.FIRST_COMPLETED)
        if abandoned in done:
            stop.set()
            await running
            return
        result = await running
        if progress_sender is not None:
            progress_sender.cancel()
            await asyncio.gather(progress_sender, return_exceptions=True)
            progress_sender = None
        result["effective_timeout_seconds"] = timeout
        observation.emit("process_result", exit_code=result.get("exit_code"),
                         timed_out=result.get("timed_out"),
                         output_incomplete=result.get("output_incomplete", False),
                         stdout_truncated=result.get("stdout_truncated", False),
                         stderr_truncated=result.get("stderr_truncated", False))
        await write_frame(writer, {'ok': True, 'result': result})
        # One result frame per request: nothing is written after it.
        # Keep the cgroup present until the client has sampled its counters.
        # EOF acknowledges receipt; a client that stays silent (wedged) is
        # waited for at most RECEIPT_SECONDS, then the runner ends anyway.
        try:
            await asyncio.wait_for(abandoned, RECEIPT_SECONDS)
        except TimeoutError:
            pass
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    except Exception as error:
        try:
            await write_frame(writer, {'ok': False, 'error': f'{type(error).__name__}: {error}'})
        except ConnectionError:
            pass
    finally:
        if progress_sender is not None:
            progress_sender.cancel()
            await asyncio.gather(progress_sender, return_exceptions=True)
        if observation is not None:
            observation.close()
        if abandoned is not None:
            abandoned.cancel()
            await asyncio.gather(abandoned, return_exceptions=True)
        if running is not None and not running.done():
            stop.set()
            await running
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass
    # Returning ends the service's main process. systemd cleans its entire cgroup.


def main() -> None:
    from .settings import EnvironmentSettings
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--section', choices=['execution'])
    args = parser.parse_args()
    if os.getuid() == 0:
        raise SystemExit('Task runners must never run as root')
    # Fixed root-controlled site file, not a model-supplied execution request.
    for path in (args.config, *args.config.parents):
        info = path.lstat()
        if path.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
            raise PermissionError('Task config ancestry must be root controlled')
    from .protocol_json import decode_object
    raw = decode_object(args.config.read_bytes())
    settings = EnvironmentSettings.from_mapping(raw[args.section] if args.section else raw)
    asyncio.run(serve(settings))
