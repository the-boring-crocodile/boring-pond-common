"""File edits land on disk: content and mode, stale hashes change nothing, the edit lock is released."""
import fcntl
import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path

from boring_worker_kit.file_policy import FilePolicy
from boring_worker_kit.files import FileTools
from boring_worker_kit.outcomes import Refused


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FileToolsEffectTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "work"
        self.root.mkdir()
        self.locks = Path(self.temp.name) / "locks"
        policy = FilePolicy.parse({"roots": {"work": {"path": str(self.root), "read": True, "write": True}}})
        self.tools = FileTools(policy, self.locks)

    def tearDown(self):
        self.tools.pool.shutdown(wait=True)
        self.temp.cleanup()

    def leftovers(self):
        return [p.name for p in self.root.iterdir() if p.name.startswith(".boring-file-")]

    def assert_locks_released(self):
        for lock in self.locks.iterdir():
            fd = os.open(lock, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises BlockingIOError while held
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    async def test_every_read_and_write_carries_refused_file_kind(self):
        import socket
        (self.root / "folder").mkdir()
        (self.root / "regular").write_text("original")
        (self.root / "link").symlink_to(self.root / "regular")
        os.mkfifo(self.root / "fifo")
        server = socket.socket(socket.AF_UNIX)
        self.addCleanup(server.close)
        server.bind(str(self.root / "socket"))
        operations = (("file_read", {}), ("file_read_bytes", {}),
                      ("file_write", {"content": "new", "expected_sha256": None}),
                      ("file_write_bytes", {"data": "", "expected_sha256": None}),
                      ("file_patch", {"expected_sha256": sha(b"original"),
                                      "edits": [{"old_text": "original", "new_text": "new"}]}))
        for name, kind, message in (("folder", "directory", "use file_list"),
                                   ("link", "symlink", "do not follow links"),
                                   ("fifo", "other", "FIFOs"), ("socket", "other", "sockets")):
            for operation, extra in operations:
                with self.subTest(name=name, operation=operation), self.assertRaises(Refused) as caught:
                    await self.tools.call(operation, {"root": "work", "path": name, **extra})
                outcome = caught.exception.as_data()
                self.assertEqual((outcome["status"], outcome["code"], outcome["kind"]),
                                 ("refused", "forbidden", kind))
                self.assertIn(message, outcome["message"])
            with self.subTest(name=name, operation="read_whole"), self.assertRaises(Refused) as caught:
                await self.tools.read_whole("work", name, 100)
            self.assertEqual(caught.exception.as_data()["kind"], kind)
        self.assertEqual((self.root / "regular").read_text(), "original")

    async def test_hard_link_write_has_distinct_wording(self):
        (self.root / "a").write_text("original")
        os.link(self.root / "a", self.root / "b")
        for operation, extra in (
                ("file_write", {"content": "new", "expected_sha256": sha(b"original")}),
                ("file_write_bytes", {"data": "", "expected_sha256": sha(b"original")}),
                ("file_patch", {"expected_sha256": sha(b"original"),
                                "edits": [{"old_text": "original", "new_text": "new"}]})):
            with self.subTest(operation=operation), self.assertRaises(Refused) as caught:
                await self.tools.call(operation, {"root": "work", "path": "a", **extra})
            outcome = caught.exception.as_data()
            self.assertEqual((outcome["code"], outcome["kind"]), ("forbidden", "other"))
            self.assertIn("multiple hard links", outcome["message"])
        self.assertEqual((self.root / "b").read_text(), "original")

    async def test_every_write_names_missing_folder_and_how_to_recover(self):
        (self.root / "present").mkdir()
        for operation, extra in (
                ("file_write", {"content": "new", "expected_sha256": None}),
                ("file_write_bytes", {"data": "", "expected_sha256": None}),
                ("file_patch", {"expected_sha256": sha(b"old"),
                                "edits": [{"old_text": "old", "new_text": "new"}]})):
            with self.subTest(operation=operation), self.assertRaises(Refused) as caught:
                await self.tools.call(operation, {"root": "work",
                    "path": "present/missing/deeper/file", **extra})
            outcome = caught.exception.as_data()
            self.assertEqual((outcome["status"], outcome["code"], outcome["path"]),
                             ("refused", "not_found", "present/missing"))
            self.assertIn("folder 'present/missing' does not exist", outcome["message"])
            self.assertIn(f"{operation} does not create folders", outcome["message"])
            self.assertIn("create it first or choose another path", outcome["message"])
        self.assertFalse((self.root / "present" / "missing").exists())
        self.assertFalse(self.locks.exists())

    async def test_missing_read_folder_keeps_read_wording(self):
        with self.assertRaises(Refused) as caught:
            await self.tools.call("file_read", {"root": "work", "path": "missing/file"})
        self.assertEqual(caught.exception.message, "directory does not exist")

    async def test_new_file_lands_with_content_and_default_mode(self):
        result = await self.tools.call("file_write", {"root": "work", "path": "new.txt",
                                                      "content": "hello\n", "expected_sha256": None})
        target = self.root / "new.txt"
        self.assertEqual(target.read_bytes(), b"hello\n")
        self.assertEqual(result["sha256"], sha(b"hello\n"))
        self.assertEqual(result["bytes"], 6)
        umask = os.umask(0)
        os.umask(umask)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o666 & ~umask)
        self.assertEqual(self.leftovers(), [])

    async def test_replacement_keeps_the_existing_mode(self):
        target = self.root / "kept.txt"
        target.write_bytes(b"one\n")
        target.chmod(0o640)
        result = await self.tools.call("file_write", {"root": "work", "path": "kept.txt",
                                                      "content": "two\n", "expected_sha256": sha(b"one\n")})
        self.assertEqual(target.read_bytes(), b"two\n")
        self.assertEqual(result["sha256"], sha(b"two\n"))
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
        self.assertEqual(self.leftovers(), [])

    async def test_stale_hash_leaves_the_file_untouched(self):
        target = self.root / "keep.txt"
        target.write_bytes(b"original\n")
        target.chmod(0o600)
        before = target.stat()
        for operation, extra in (("file_write", {"content": "replaced\n"}),
                                 ("file_patch", {"edits": [{"old_text": "original", "new_text": "patched"}]})):
            with self.subTest(operation=operation):
                with self.assertRaises(Refused) as caught:
                    await self.tools.call(operation, {"root": "work", "path": "keep.txt",
                                                      "expected_sha256": sha(b"something else"), **extra})
                self.assertEqual(caught.exception.code, "stale")
                self.assertEqual(caught.exception.details["current_sha256"], sha(b"original\n"))
                self.assertEqual(target.read_bytes(), b"original\n")
                after = target.stat()
                self.assertEqual((after.st_ino, stat.S_IMODE(after.st_mode), after.st_mtime_ns),
                                 (before.st_ino, stat.S_IMODE(before.st_mode), before.st_mtime_ns))
                self.assertEqual(self.leftovers(), [])
                self.assert_locks_released()

    async def test_edit_right_after_an_edit_succeeds(self):
        first = await self.tools.call("file_write", {"root": "work", "path": "seq.txt",
                                                     "content": "a\n", "expected_sha256": None})
        self.assert_locks_released()
        second = await self.tools.call("file_patch", {"root": "work", "path": "seq.txt",
                                                      "expected_sha256": first["sha256"],
                                                      "edits": [{"old_text": "a", "new_text": "b"}]})
        self.assertEqual(second["applied_edits"], 1)
        self.assertEqual((self.root / "seq.txt").read_bytes(), b"b\n")
        third = await self.tools.call("file_write", {"root": "work", "path": "seq.txt",
                                                     "content": "c\n", "expected_sha256": second["sha256"]})
        self.assertEqual((self.root / "seq.txt").read_bytes(), b"c\n")
        self.assertEqual(third["sha256"], sha(b"c\n"))
        self.assert_locks_released()
        self.assertEqual(self.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
