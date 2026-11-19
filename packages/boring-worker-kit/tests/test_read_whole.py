import asyncio
import hashlib
import os
import threading

import pytest

from boring_outcomes import Refused
from boring_worker_kit.file_policy import FilePolicy
from boring_worker_kit.files import FileTools


@pytest.fixture
def tools(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    policy = FilePolicy.parse({"roots": {"work": {"path": str(root), "read": True,
                                                 "write": True, "deny": ["private"]}},
                               "limits": {"max_concurrent_operations": 1}})
    value = FileTools(policy, tmp_path / "locks", reserved_prefixes=(".fixture-",))
    yield value, root
    value.pool.shutdown(wait=True)


def test_caller_cap_is_independent_of_text_limit_and_returns_exact_hash(tools):
    value, root = tools
    content = b"\x00\xff" * 300000
    (root / "binary").write_bytes(content)
    async def scenario():
        assert await value.read_whole("work", "binary", len(content)) == (
            content, hashlib.sha256(content).hexdigest())
        with pytest.raises(Refused) as error:
            await value.read_whole("work", "binary", len(content) - 1)
        assert error.value.code == "limit"
        with pytest.raises(Refused) as error:
            await value.call("file_read_bytes", {"root": "work", "path": "binary"})
        assert error.value.code == "limit"
    asyncio.run(scenario())


@pytest.mark.parametrize("cap", [0, -1, True, 1.5, None])
def test_caller_cap_is_a_positive_integer(tools, cap):
    value, root = tools
    async def scenario():
        with pytest.raises(Refused) as error:
            await value.read_whole("work", "missing", cap)
        assert error.value.code == "invalid_request"
    asyncio.run(scenario())


@pytest.mark.parametrize("path,code", [
    ("missing", "not_found"), ("missing/file", "not_found"),
    ("../outside", "forbidden"), ("private/file", "forbidden"),
    (".fixture-internal", "forbidden"), ("link", "forbidden"),
    ("directory", "forbidden"), ("pipe", "forbidden"),
])
def test_whole_read_uses_existing_path_and_regular_file_policy(tools, path, code):
    value, root = tools
    (root / "target").write_bytes(b"data")
    (root / "link").symlink_to(root / "target")
    (root / "directory").mkdir()
    os.mkfifo(root / "pipe")
    async def scenario():
        with pytest.raises(Refused) as error:
            await value.read_whole("work", path, 100)
        assert error.value.code == code
    asyncio.run(scenario())


def test_read_permission_and_unknown_root(tools):
    value, root = tools
    (root / "data").write_bytes(b"ok")
    policy = FilePolicy.parse({"roots": {"work": {"path": str(root), "read": False, "write": False}}})
    denied = FileTools(policy, root.parent / "other-locks")
    async def scenario():
        for tool, label in ((value, "absent"), (denied, "work")):
            with pytest.raises(Refused) as error:
                await tool.read_whole(label, "data", 2)
            assert error.value.code == "forbidden"
    try:
        asyncio.run(scenario())
    finally:
        denied.pool.shutdown(wait=True)


def test_cancelled_whole_read_retains_shared_slot_until_thread_settles(tools, monkeypatch):
    value, root = tools
    started, release = threading.Event(), threading.Event()
    original = value._read_whole
    def blocked(*args):
        started.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(value, "_read_whole", blocked)
    (root / "data").write_bytes(b"ok")
    async def scenario():
        task = asyncio.create_task(value.read_whole("work", "data", 2))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert value.slots.locked()
            # Ordinary file calls queue behind the same slot.
            next_call = asyncio.create_task(value.call("file_read_bytes", {"root": "work", "path": "data"}))
            await asyncio.sleep(0)
            assert not next_call.done()
            release.set()
            result = await asyncio.wait_for(next_call, 2)
            assert result["length"] == 2
        finally:
            release.set()
    asyncio.run(scenario())
