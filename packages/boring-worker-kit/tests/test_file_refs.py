"""File references and save_to around one service call, within a carried deadline.

The call itself is a fake here; the Crocodile and Pirates test it against real
services."""
import asyncio
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from boring_worker_kit import deadlines, file_refs
from boring_worker_kit.file_policy import FilePolicy
from boring_worker_kit.files import FileTools
from boring_worker_kit.outcomes import Refusal
from boring_worker_kit.services import CALL_TIMEOUT_SECONDS, Services


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def ref(path, **extra):
    return {"$file": {"root": "work", "path": path, **extra}}


class FileRefsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.work, self.notes = base / "work", base / "notes"
        self.work.mkdir()
        self.notes.mkdir()
        policy = FilePolicy.parse({"roots": {"work": {"path": str(self.work), "read": True, "write": True},
                                             "notes": {"path": str(self.notes), "read": True, "write": False}}})
        self.files = FileTools(policy, base / "locks")
        self.calls = []

    def tearDown(self):
        self.files.pool.shutdown(wait=True)
        self.temp.cleanup()

    async def echo(self, arguments, timeout):
        self.calls.append((arguments, timeout))
        return {"content": [{"type": "text", "text": json.dumps(arguments)}]}

    async def run_call(self, arguments, save_to=None, deadline=None):
        return await file_refs.call_service(self.files, arguments, save_to, self.echo, deadline)

    async def test_references_become_exact_text(self):
        (self.work / "a.svg").write_bytes("﻿<svg>\r\n" .encode() + b"<rect/>" * 15_000 + b"\r\n</svg>")
        answer = await self.run_call({"svg": ref("a.svg"), "n": [1]})
        sent = self.calls[0][0]["svg"]
        self.assertTrue(sent.startswith("<svg>\r\n"))  # BOM dropped, CRLF kept, one 105 KB line fine
        self.assertEqual(len(sent.encode()), len((self.work / "a.svg").read_bytes()) - 3)
        self.assertEqual(json.loads(answer["content"][0]["text"])["n"], [1])

    async def test_references_obey_rules_and_nothing_is_sent(self):
        (self.work / "a.txt").write_text("x")
        (self.work / "latin.txt").write_bytes(b"caf\xe9")
        for arguments, code in (({"v": ref("missing.txt")}, "not_found"),
                                ({"v": ref("a.txt", sha256="0" * 64)}, "stale"),
                                ({"v": ref("latin.txt")}, "invalid_request"),
                                ({"v": {"$file": "a.txt"}}, "invalid_request")):
            with self.subTest(arguments=arguments), self.assertRaises(Refusal) as caught:
                await self.run_call(arguments)
            self.assertEqual(caught.exception.code, code)
        with patch.object(file_refs, "MAX_REFERENCE_BYTES", 1), self.assertRaises(Refusal) as caught:
            await self.run_call({"v": ref("a.txt"), "w": ref("a.txt")})
        self.assertEqual(caught.exception.code, "limit")
        self.assertEqual(self.calls, [])

    async def test_save_to_writes_a_receipt_and_checks_the_target_first(self):
        (self.work / "in.txt").write_text("hello")
        receipt = await self.run_call({"v": ref("in.txt")}, {"root": "work", "path": "out.txt", "expected_sha256": None})
        self.assertEqual(json.loads((self.work / "out.txt").read_text()), {"v": "hello"})
        self.assertEqual(receipt["file_references"], [{"root": "work", "path": "in.txt", "bytes": 5}])
        self.assertNotIn("content", receipt)
        for save_to, code in (({"root": "work", "path": "out.txt", "expected_sha256": None}, "stale"),
                              ({"root": "work", "path": "no/dir/x.txt", "expected_sha256": None}, "not_found"),
                              ({"root": "work", "path": "x.txt"}, "invalid_request")):
            with self.subTest(save_to=save_to), self.assertRaises(Refusal) as caught:
                await self.run_call({}, save_to)
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(Refusal):  # a read-only root
            await self.run_call({}, {"root": "notes", "path": "n.txt", "expected_sha256": None})
        self.assertEqual(len(self.calls), 1)  # only the first call reached the service

    async def test_an_answer_survives_a_failed_save(self):
        real = self.files.call

        async def disk_full(operation, request, **kw):
            if operation == "file_write":
                raise OSError(28, "No space left on device")
            return await real(operation, request, **kw)
        with patch.object(self.files, "call", disk_full):
            result = await self.run_call({"v": 1}, {"root": "work", "path": "full.txt", "expected_sha256": None})
        self.assertIn("content", result)
        self.assertEqual(result["save_to_failed"]["effects"], "unknown")  # a failure, not a refusal
        self.assertIn("No space left", result["save_to_failed"]["message"])
        self.assertNotIn("save_to_refused", result)

    async def test_the_service_gets_what_is_left_of_the_deadline(self):
        await self.run_call({})
        self.assertEqual(self.calls[-1][1], CALL_TIMEOUT_SECONDS)  # no deadline: the service contract
        await self.run_call({}, deadline=deadlines.deadline_after(12.0))
        self.assertLess(self.calls[-1][1], 12.0)
        self.assertGreater(self.calls[-1][1], 11.0)
        await self.run_call({}, {"root": "work", "path": "s.txt", "expected_sha256": None},
                            deadline=deadlines.deadline_after(12.0))
        self.assertLess(self.calls[-1][1], 12.0 - deadlines.REPLY_MARGIN_SECONDS + 0.01)  # kept back for save_to
        await self.run_call({}, deadline=deadlines.deadline_after(300.0))
        self.assertEqual(self.calls[-1][1], CALL_TIMEOUT_SECONDS)  # never more than the contract
        count = len(self.calls)
        with self.assertRaises(Refusal) as caught:
            await self.run_call({}, deadline=deadlines.deadline_after(0.0))
        self.assertEqual(caught.exception.code, "limit")
        self.assertEqual(len(self.calls), count)


    async def test_slow_reads_before_the_call_count_against_the_deadline(self):
        (self.work / "a.txt").write_text("x")
        real = self.files.call

        async def slow(operation, request, **kw):
            await asyncio.sleep(1)
            return await real(operation, request, **kw)
        with patch.object(self.files, "call", slow), self.assertRaises(Refusal) as caught:
            await self.run_call({"v": ref("a.txt")}, deadline=deadlines.deadline_after(0.2))
        self.assertEqual(caught.exception.code, "limit")
        self.assertIn("nothing was sent", caught.exception.message)
        self.assertEqual(self.calls, [])

    async def test_a_save_that_does_not_finish_returns_the_answer_and_reports_late(self):
        # The write thread is already running (the file tools shield it): stopping the
        # wait does not stop it, so the file appears later and its receipt is reported.
        release, real = threading.Event(), self.files._file_call

        def slow_write(operation, request):
            if operation == "file_write":
                release.wait(5)
            return real(operation, request)
        late = []
        with patch.object(file_refs, "REPLY_MARGIN_SECONDS", 0.2), patch.object(file_refs, "SAVE_FLOOR_SECONDS", 0.1), \
                patch.object(self.files, "_file_call", slow_write):
            result = await file_refs.call_service(
                self.files, {"v": 1}, {"root": "work", "path": "slow.txt", "expected_sha256": None}, self.echo,
                deadlines.deadline_after(0.5), on_late_save=late.append)
            self.assertIn("content", result)  # the service ran; its answer is not lost
            self.assertEqual(result["save_to_failed"]["effects"], "unknown")  # it may still land
            self.assertFalse((self.work / "slow.txt").exists())
            release.set()
            for _ in range(200):
                if late:
                    break
                await asyncio.sleep(0.01)
        self.assertTrue((self.work / "slow.txt").exists())
        self.assertEqual(late[0]["saved_to"]["path"], "slow.txt")
        self.assertEqual(late[0]["landed_after"], "timeout")

    async def test_a_cancelled_save_settles_its_write_and_reports_it(self):
        # Cancellation (a stopped assignment, an outer deadline) waits for the short local
        # write, as a settled file tool call does: the caller can count it before it answers.
        release, real = threading.Event(), self.files._file_call

        def slow_write(operation, request):
            if operation == "file_write":
                release.wait(5)
            return real(operation, request)
        late = []
        with patch.object(self.files, "_file_call", slow_write):
            task = asyncio.ensure_future(file_refs.call_service(
                self.files, {"v": 1}, {"root": "work", "path": "c.txt", "expected_sha256": None}, self.echo,
                on_late_save=late.append))
            for _ in range(200):
                if self.calls:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)  # now inside the write
            task.cancel()
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())  # still waiting for the write
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue((self.work / "c.txt").exists())
        self.assertEqual((late[0]["saved_to"]["path"], late[0]["landed_after"]), ("c.txt", "cancellation"))

    async def test_a_cancelled_save_waits_a_bounded_time(self):
        # A hung write does not hold a cancelled caller longer than the reply margin.
        release, real = threading.Event(), self.files._file_call

        def stuck(operation, request):
            if operation == "file_write":
                release.wait(5)
            return real(operation, request)
        late = []
        with patch.object(file_refs, "REPLY_MARGIN_SECONDS", 0.2), patch.object(self.files, "_file_call", stuck):
            task = asyncio.ensure_future(file_refs.call_service(
                self.files, {"v": 1}, {"root": "work", "path": "h.txt", "expected_sha256": None}, self.echo,
                on_late_save=late.append))
            while not self.calls:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            started = asyncio.get_running_loop().time()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertLess(asyncio.get_running_loop().time() - started, 1.0)
            release.set()
            while not late:
                await asyncio.sleep(0.01)
        self.assertEqual(late[0]["landed_after"], "timeout")

    async def test_a_settled_save_ignores_the_deadline(self):
        # settle_save: the save is a file write like any other (a Pirate's), waited for in full.
        release, real = threading.Event(), self.files._file_call

        def slow(operation, request):
            if operation == "file_write":
                release.wait(5)
            return real(operation, request)
        threading.Timer(0.6, release.set).start()
        with patch.object(file_refs, "REPLY_MARGIN_SECONDS", 0.1), patch.object(self.files, "_file_call", slow):
            result = await file_refs.call_service(
                self.files, {"v": 1}, {"root": "work", "path": "s.txt", "expected_sha256": None}, self.echo,
                deadlines.deadline_after(0.3), settle_save=True)
        self.assertEqual(result["saved_to"]["path"], "s.txt")

    async def test_arguments_must_be_an_object_not_a_reference(self):
        (self.work / "a.txt").write_text("x")
        read = []
        real = self.files.call

        async def counting(operation, request, **kw):
            read.append(operation)
            return await real(operation, request, **kw)
        with patch.object(self.files, "call", counting), self.assertRaises(Refusal) as caught:
            await self.run_call(ref("a.txt"))
        self.assertEqual((caught.exception.code, read, self.calls), ("invalid_request", [], []))

    def test_has_references_uses_the_resolve_rule(self):
        self.assertTrue(file_refs.has_references({"a": [{"b": ref("x")}]}))
        self.assertFalse(file_refs.has_references({"a": {"$file": {"root": "w", "path": "p"}, "other": 1}}))
        self.assertFalse(file_refs.has_references({"a": "$file"}))

    async def test_the_inline_answer_limit_is_the_callers(self):
        real = self.files.call

        async def refused(operation, request, **kw):
            if operation == "file_write":
                raise Refusal("forbidden", "no")
            return await real(operation, request, **kw)
        with patch.object(self.files, "call", refused):
            result = await file_refs.call_service(
                self.files, {"v": "x" * 2000}, {"root": "work", "path": "r.txt", "expected_sha256": None},
                self.echo, max_inline_answer=1000)
        self.assertNotIn("content", result)
        self.assertTrue(result["service_ran"])
        self.assertEqual(result["save_to_refused"]["code"], "forbidden")


class DeadlineTest(unittest.TestCase):
    def test_budget(self):
        self.assertEqual(deadlines.budget(20, 103), 20.0)
        self.assertEqual(deadlines.budget(500, 103), 103.0)
        for bad in (None, True, "5", float("nan"), float("inf"), 10**400):
            self.assertEqual(deadlines.budget(bad, 103), 103.0)  # absent, not a number, or too large: the cap
        for none_left in (0, -1, -10**400, float("-inf")):
            self.assertEqual(deadlines.budget(none_left, 103), 0.0)  # never widened to the cap
        self.assertEqual(deadlines.SERVICE_OPERATION_SECONDS, CALL_TIMEOUT_SECONDS + 15.0)


class ServiceTimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_call_waits_at_most_the_time_given(self):
        async def silent(reader, writer):
            # Never answers; holds the connection until the client goes away, then
            # closes it. Since Python 3.12 Server.wait_closed() waits for open
            # connections, so a handler that never closes would hang the cleanup.
            try:
                await reader.read(1)
                await reader.read()
            finally:
                writer.close()
        server = await asyncio.start_server(silent, "127.0.0.1", 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        port = server.sockets[0].getsockname()[1]
        services = Services("fixture")
        self.addAsyncCleanup(services.close)
        services.register({"name": "quiet", "url": f"http://127.0.0.1:{port}/mcp"})
        with self.assertRaisesRegex(TimeoutError, r"within 0\.3 s"):
            await services.call("quiet", "x", {}, "token", timeout=0.3)
        with self.assertRaises(Refusal) as caught:
            await services.call("quiet", "x", {}, "token", timeout=0)
        self.assertEqual(caught.exception.code, "limit")
