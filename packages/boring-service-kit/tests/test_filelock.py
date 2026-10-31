"""Real process ordering: an empty-file observation may predate another holder's lock."""
import multiprocessing
import os
from pathlib import Path

import pytest
from boring_service_kit.filelock import file_lock


def test_empty_lock_needs_no_initialization_write(tmp_path):
    path = tmp_path / "empty.lock"
    with file_lock(path):
        assert path.stat().st_size == 0
    with file_lock(path):
        assert path.stat().st_size == 0
    assert path.read_bytes() == b""


def _holder(path, held, release, results):
    try:
        with file_lock(Path(path)):
            held.set()
            if not release.wait(15):
                raise RuntimeError("holder release timed out")
        results.put("holder-ok")
    except BaseException as error:
        results.put("holder-" + type(error).__name__)


def _stale_waiter(path, observed, held, done, results):
    from boring_service_kit import filelock
    original = os.fstat
    first = True
    def stale_fstat(fd):
        nonlocal first
        info = original(fd)
        if first:
            first = False
            assert info.st_size == 0
            observed.set()
            if not held.wait(15):
                raise RuntimeError("holder did not lock")
        return info
    filelock.os.fstat = stale_fstat
    try:
        with file_lock(Path(path)):
            results.put("waiter-ok")
    except BaseException as error:
        results.put("waiter-" + type(error).__name__)
    finally:
        filelock.os.fstat = original
        done.set()


@pytest.mark.skipif(os.name != "nt", reason="Windows byte-range lock interleaving")
def test_stale_empty_observation_does_not_write_into_another_process_lock(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    observed, held, release, done = (ctx.Event() for _ in range(4))
    results = ctx.Queue()
    path = str(tmp_path / "race.lock")
    waiter = ctx.Process(target=_stale_waiter, args=(path, observed, held, done, results))
    holder = ctx.Process(target=_holder, args=(path, held, release, results))
    try:
        waiter.start()
        assert observed.wait(15), "waiter did not observe the empty lock"
        holder.start()
        assert held.wait(15), "holder did not acquire the lock"
        # The waiter must block, not fail trying to initialize an already-locked byte.
        early = done.wait(0.3)
        release.set()
        waiter.join(15)
        holder.join(15)
        assert not waiter.is_alive() and not holder.is_alive()
        outcomes = sorted(results.get(timeout=3) for _ in range(2))
        assert outcomes == ["holder-ok", "waiter-ok"], outcomes
        assert not early, "waiter completed while another process still held the lock"
        assert Path(path).read_bytes() == b""
    finally:
        release.set()
        for process in (waiter, holder):
            if process.pid:
                process.join(2)
                if process.is_alive():
                    process.kill()
                    process.join(5)
        results.close()
