"""Edge cases of the descriptor-relative primitives.

Each test names the claim it checks. They run as an ordinary user in a
temporary directory; nothing here needs root.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import boring_file_core as core
from boring_file_core import (InvalidPath, NotAllowedFile, OutsideScope, StaleEdit, TooLarge,
                              atomic_write, digest, directory_fd, parts, read_regular, root_fd,
                              verify_target)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "root"
        self.root.mkdir()
        self.outside = Path(self.temp.name) / "outside"
        self.outside.mkdir()
        (self.outside / "secret").write_text("outside")


class PartsTest(Base):
    def test_well_formed_relative_paths_split(self) -> None:
        self.assertEqual(parts("a/b.txt"), ("a", "b.txt"))
        self.assertEqual(parts(".", directory=True), ())
        self.assertEqual(parts(".hidden"), (".hidden",))

    def test_malformed_paths_are_invalid(self) -> None:
        for bad in ("", "/etc/passwd", "a" * 1025, 7, None):
            with self.subTest(path=bad), self.assertRaises(InvalidPath):
                parts(bad)  # type: ignore[arg-type]

    def test_length_limits_count_utf8_bytes(self) -> None:
        self.assertEqual(len(parts("a" * 255)), 1)
        self.assertEqual(len(parts("/".join(["a" * 255] * 4))), 4)  # 1023 bytes
        with self.assertRaises(InvalidPath):
            parts("ä" * 513)  # 1026 bytes
        with self.assertRaises(InvalidPath):
            parts("a" * 256)  # one component over NAME_MAX (0.2.1)
        with self.assertRaises(InvalidPath):
            parts("ä" * 128)  # 256 bytes in one component

    def test_unencodable_path_is_invalid_not_a_crash(self) -> None:
        with self.assertRaises(InvalidPath):
            parts("a\udcffb")  # a lone surrogate (0.2.1)

    def test_traversal_and_reserved_names_are_outside_scope(self) -> None:
        for bad in ("..", "a/../b", "a/./b", "a//b", "a/", ".git/config", "x/.git",
                    "a/.boring-file-x", "a\x00b", "."):
            with self.subTest(path=bad), self.assertRaises(OutsideScope):
                parts(bad)

    def test_consumer_reserved_prefixes_are_opt_in(self) -> None:
        self.assertEqual(parts(".pirate-tmp"), (".pirate-tmp",))
        with self.assertRaises(OutsideScope):
            parts("a/.pirate-tmp", reserved=(".pirate-",))

    def test_typed_outcomes_stay_value_errors(self) -> None:
        for kind in (InvalidPath, OutsideScope, NotAllowedFile, TooLarge, StaleEdit):
            self.assertTrue(issubclass(kind, ValueError))


class DirectoryTest(Base):
    def test_symlinked_directory_is_not_followed(self) -> None:
        (self.root / "link").symlink_to(self.outside)
        with root_fd(self.root) as fd, self.assertRaises(OSError):
            with directory_fd(fd, ("link",)):
                pass

    def test_symlinked_root_is_not_followed(self) -> None:
        link = Path(self.temp.name) / "rootlink"
        link.symlink_to(self.root)
        with self.assertRaises(OSError), root_fd(link):
            pass

    def test_nested_directories_resolve_and_descriptors_close(self) -> None:
        (self.root / "a" / "b").mkdir(parents=True)
        (self.root / "a" / "b" / "f").write_text("x")
        before = len(os.listdir("/proc/self/fd"))
        with root_fd(self.root) as fd, directory_fd(fd, ("a", "b")) as inner:
            self.assertEqual(read_regular(inner, "f", 10)[0], b"x")
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    def test_file_in_the_middle_of_a_path_is_refused(self) -> None:
        (self.root / "f").write_text("x")
        with root_fd(self.root) as fd, self.assertRaises(NotADirectoryError):
            with directory_fd(fd, ("f",)):
                pass


class ReadTest(Base):
    def read(self, name: str, maximum: int = 100, **kwargs: bool) -> bytes:
        with root_fd(self.root) as fd:
            return read_regular(fd, name, maximum, **kwargs)[0]

    def test_symlinked_file_is_not_followed(self) -> None:
        (self.root / "link").symlink_to(self.outside / "secret")
        with self.assertRaises(OSError):
            self.read("link")

    def test_fifo_is_refused_without_blocking(self) -> None:
        os.mkfifo(self.root / "pipe")
        done = threading.Event()
        def attempt() -> None:
            with self.assertRaises((NotAllowedFile, OSError)):
                self.read("pipe")
            done.set()
        thread = threading.Thread(target=attempt, daemon=True)
        thread.start()
        self.assertTrue(done.wait(2), "reading a FIFO blocked")

    def test_socket_is_not_allowed(self) -> None:
        import socket
        server = socket.socket(socket.AF_UNIX)
        self.addCleanup(server.close)
        server.bind(str(self.root / "sock"))
        with self.assertRaises(NotAllowedFile):  # was a bare ENXIO before 0.2.1
            self.read("sock")

    def test_directory_is_not_a_regular_file(self) -> None:
        (self.root / "d").mkdir()
        with self.assertRaises((NotAllowedFile, OSError)):
            self.read("d")

    def test_size_limit_is_exact(self) -> None:
        (self.root / "f").write_bytes(b"x" * 10)
        self.assertEqual(self.read("f", 10), b"x" * 10)
        with self.assertRaises(TooLarge):
            self.read("f", 9)

    def test_growth_after_stat_is_still_bounded(self) -> None:
        (self.root / "f").write_bytes(b"x" * 5)
        real_fstat = os.fstat
        def small_fstat(fd: int) -> os.stat_result:
            info = real_fstat(fd)
            os.write(os.open(self.root / "f", os.O_WRONLY | os.O_APPEND), b"y" * 20)
            return info
        with mock.patch.object(core.os, "fstat", small_fstat), self.assertRaises(TooLarge):
            self.read("f", 10)

    def test_hard_link_refused_only_when_single_link_required(self) -> None:
        (self.root / "f").write_text("x")
        os.link(self.root / "f", self.root / "g")
        self.assertEqual(self.read("f"), b"x")
        with self.assertRaises(NotAllowedFile):
            self.read("f", single_link=True)


class WriteTest(Base):
    def write(self, name: str, payload: bytes, *, create: bool, mode: int = 0o640) -> None:
        with root_fd(self.root) as fd:
            atomic_write(fd, name, payload, mode, create=create, prefix=".boring-file-")

    def leftovers(self) -> list[str]:
        return [n for n in os.listdir(self.root) if n.startswith(".boring-file-")]

    def test_create_writes_exact_bytes_and_mode(self) -> None:
        old = os.umask(0o077)
        try:
            self.write("new", b"hello", create=True, mode=0o644)
        finally:
            os.umask(old)
        self.assertEqual((self.root / "new").read_bytes(), b"hello")
        self.assertEqual(stat.S_IMODE((self.root / "new").stat().st_mode), 0o644)
        self.assertEqual(self.leftovers(), [])

    def test_normal_creation_obeys_umask(self) -> None:
        for mask, expected in ((0o077, 0o600), (0o027, 0o640), (0o007, 0o660)):
            old = os.umask(mask)
            try:
                with root_fd(self.root) as fd:
                    atomic_write(fd, f"new-{mask}", b"hello", None, create=True, prefix=".boring-file-")
            finally:
                os.umask(old)
            self.assertEqual(stat.S_IMODE((self.root / f"new-{mask}").stat().st_mode), expected)
        self.assertEqual(self.leftovers(), [])

    def test_normal_creation_mode_cannot_replace(self) -> None:
        (self.root / "f").write_bytes(b"original")
        with root_fd(self.root) as fd, self.assertRaises(ValueError):
            atomic_write(fd, "f", b"changed", None, create=False, prefix=".boring-file-")
        self.assertEqual((self.root / "f").read_bytes(), b"original")
        self.assertEqual(self.leftovers(), [])

    @unittest.skipUnless(shutil.which("setfacl"), "POSIX ACL tools needed")
    def test_normal_creation_keeps_default_acl_mask(self) -> None:
        subprocess.run(["setfacl", "-m", f"d:u::rwx,d:g::---,d:g:{os.getgid()}:r--,d:m::r--,d:o::---",
                        str(self.root)], check=True)
        old = os.umask(0o077)
        try:
            reference = self.root / "ordinary"
            descriptor = os.open(reference, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            os.close(descriptor)
            with root_fd(self.root) as fd:
                atomic_write(fd, "new", b"hello", None, create=True, prefix=".boring-file-")
        finally:
            os.umask(old)
        actual = self.root / "new"
        self.assertEqual(stat.S_IMODE(actual.stat().st_mode), 0o640)
        self.assertEqual(actual.stat().st_gid, reference.stat().st_gid)
        self.assertEqual(os.getxattr(actual, "system.posix_acl_access"),
                         os.getxattr(reference, "system.posix_acl_access"))

    def test_create_never_replaces_a_file_that_appeared(self) -> None:
        (self.root / "f").write_text("theirs")
        with self.assertRaises(StaleEdit):
            self.write("f", b"mine", create=True)
        self.assertEqual((self.root / "f").read_text(), "theirs")
        self.assertEqual(self.leftovers(), [])

    def test_create_never_replaces_a_symlink_that_appeared(self) -> None:
        (self.root / "f").symlink_to(self.outside / "secret")
        with self.assertRaises(StaleEdit):
            self.write("f", b"mine", create=True)
        self.assertEqual((self.outside / "secret").read_text(), "outside")

    def test_replace_swaps_a_symlink_itself_never_its_target(self) -> None:
        (self.root / "f").symlink_to(self.outside / "secret")
        self.write("f", b"mine", create=False)
        self.assertEqual((self.outside / "secret").read_text(), "outside")
        self.assertFalse((self.root / "f").is_symlink())
        self.assertEqual((self.root / "f").read_bytes(), b"mine")
        self.assertEqual(self.leftovers(), [])

    def test_replace_breaks_a_hard_link_instead_of_writing_through_it(self) -> None:
        (self.outside / "shared").write_text("outside")
        os.link(self.outside / "shared", self.root / "f")
        self.write("f", b"mine", create=False)
        self.assertEqual((self.outside / "shared").read_text(), "outside")
        self.assertEqual((self.root / "f").read_text(), "mine")

    def test_replace_sets_the_callers_mode(self) -> None:
        (self.root / "f").write_text("old")
        os.chmod(self.root / "f", 0o600)
        self.write("f", b"new", create=False, mode=0o664)
        self.assertEqual(stat.S_IMODE((self.root / "f").stat().st_mode), 0o664)
        self.assertEqual((self.root / "f").read_bytes(), b"new")
        self.assertEqual(self.leftovers(), [])

    def test_directory_in_the_files_place_is_stale(self) -> None:
        (self.root / "f").mkdir()
        with self.assertRaises(StaleEdit):
            self.write("f", b"mine", create=False)
        self.assertTrue((self.root / "f").is_dir())
        self.assertEqual(self.leftovers(), [])

    def test_failed_write_leaves_no_temporary_file(self) -> None:
        with mock.patch.object(core.os, "fsync", side_effect=OSError(5, "I/O error")), \
                self.assertRaises(OSError):
            self.write("f", b"x", create=True)
        self.assertEqual(self.leftovers(), [])
        self.assertFalse((self.root / "f").exists())

    def test_missing_renameat2_is_an_error_not_a_silent_replace(self) -> None:
        (self.root / "f").write_text("theirs")
        class NoLibc:
            def __getattr__(self, name: str) -> object:
                raise AttributeError(name)
        with mock.patch.object(core.ctypes, "CDLL", return_value=NoLibc()), \
                self.assertRaises(OSError):
            self.write("f", b"mine", create=True)
        self.assertEqual((self.root / "f").read_text(), "theirs")
        self.assertEqual(self.leftovers(), [])


class VerifyTest(Base):
    def verify(self, name: str, expected: str | None, previous: os.stat_result | None) -> None:
        with root_fd(self.root) as fd:
            verify_target(fd, name, expected, previous, 100)

    def stat(self, name: str) -> os.stat_result:
        with root_fd(self.root) as fd:
            return read_regular(fd, name, 100)[1]

    def test_unchanged_file_passes(self) -> None:
        (self.root / "f").write_text("x")
        self.verify("f", digest(b"x"), self.stat("f"))

    def test_changed_content_is_stale(self) -> None:
        (self.root / "f").write_text("x")
        before = self.stat("f")
        (self.root / "f").write_text("y")
        with self.assertRaises(StaleEdit):
            self.verify("f", digest(b"x"), before)

    def test_replaced_file_with_same_content_is_stale(self) -> None:
        (self.root / "f").write_text("x")
        before = self.stat("f")
        (self.root / "g").write_text("x")
        os.replace(self.root / "g", self.root / "f")
        with self.assertRaises(StaleEdit):
            self.verify("f", digest(b"x"), before)

    def test_disappeared_file_is_stale_appeared_file_is_stale(self) -> None:
        (self.root / "f").write_text("x")
        before = self.stat("f")
        os.unlink(self.root / "f")
        with self.assertRaises(StaleEdit):
            self.verify("f", digest(b"x"), before)
        self.verify("f", None, None)  # still absent, as expected for a create
        (self.root / "f").write_text("new")
        with self.assertRaises(StaleEdit):
            self.verify("f", None, None)

    def test_anything_else_at_the_name_is_stale(self) -> None:
        # 0.2.1: a second link, a FIFO, a symlink, a directory or a bigger
        # file where the read file was is a change, never an untyped error.
        for make in ("link", "fifo", "symlink", "directory", "bigger"):
            with self.subTest(replaced_by=make):
                (self.root / "f").write_text("x")
                before = self.stat("f")
                if make == "link":
                    os.link(self.root / "f", self.root / "g")
                else:
                    os.unlink(self.root / "f")
                    if make == "fifo":
                        os.mkfifo(self.root / "f")
                    elif make == "symlink":
                        (self.root / "f").symlink_to(self.outside / "secret")
                    elif make == "directory":
                        (self.root / "f").mkdir()
                    else:
                        (self.root / "f").write_bytes(b"x" * 200)
                with self.assertRaises(StaleEdit):
                    self.verify("f", digest(b"x"), before)
                for name in ("f", "g"):
                    path = self.root / name
                    if path.is_dir() and not path.is_symlink():
                        path.rmdir()
                    elif path.exists() or path.is_symlink():
                        path.unlink()


@unittest.skipUnless(os.name == "posix" and shutil.which("setfacl"), "POSIX ACL tools needed")
class AccessAclTest(Base):
    def set_acl(self, path: Path, rule: str) -> None:
        subprocess.run(["setfacl", "-m", rule, str(path)], check=True, capture_output=True)

    def test_inherited_acl_preserved_across_replacement(self) -> None:
        self.set_acl(self.root, f"d:g:{os.getgid()}:r--")
        path = self.root / "f"
        path.write_bytes(b"old")
        original = os.getxattr(path, "system.posix_acl_access")
        original_mode = stat.S_IMODE(path.stat().st_mode)
        with root_fd(self.root) as parent:
            previous = read_regular(parent, "f", 100)[1]
            metadata = core.edit_metadata(parent, "f", previous)
            verify_target(parent, "f", digest(b"old"), previous, 100, metadata)
            atomic_write(parent, "f", b"new", metadata.mode, create=False,
                         prefix=".boring-file-", group=metadata.gid, metadata=metadata,
                         before_replace=lambda: verify_target(parent, "f", digest(b"old"),
                                                              previous, 100, metadata))
        self.assertEqual(path.read_bytes(), b"new")
        self.assertEqual(os.getxattr(path, "system.posix_acl_access"), original)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), original_mode)
        self.assertEqual(path.stat().st_gid, previous.st_gid)
        self.assertEqual(list(self.root.glob(".boring-file-*")), [])

    def test_metadata_changed_between_read_and_snapshot_is_stale(self) -> None:
        path = self.root / "f"
        path.write_bytes(b"old")
        with root_fd(self.root) as parent:
            _, previous = read_regular(parent, "f", 100, single_link=True)
            os.chmod(path, previous.st_mode ^ stat.S_IWGRP)
            with self.assertRaises(StaleEdit):
                core.edit_metadata(parent, "f", previous)
        self.assertEqual(path.read_bytes(), b"old")

    def test_acl_change_during_edit_is_stale_without_publication(self) -> None:
        path = self.root / "f"
        path.write_bytes(b"old")
        self.set_acl(path, f"g:{os.getgid()}:r--")
        with root_fd(self.root) as parent:
            previous = read_regular(parent, "f", 100)[1]
            metadata = core.edit_metadata(parent, "f", previous)
            self.set_acl(path, f"g:{os.getgid()}:rw-")
            with self.assertRaises(StaleEdit):
                atomic_write(parent, "f", b"new", metadata.mode, create=False,
                             prefix=".boring-file-", group=metadata.gid, metadata=metadata,
                             before_replace=lambda: verify_target(parent, "f", digest(b"old"),
                                                                  previous, 100, metadata))
        self.assertEqual(path.read_bytes(), b"old")
        self.assertEqual(list(self.root.glob(".boring-file-*")), [])

    def test_unsupported_xattr_is_still_refused(self) -> None:
        path = self.root / "f"
        path.write_bytes(b"old")
        os.setxattr(path, "user.opaque", b"keep")
        with root_fd(self.root) as parent:
            previous = read_regular(parent, "f", 100)[1]
            with self.assertRaises(core.NotAllowedMetadata):
                core.edit_metadata(parent, "f", previous)
        self.assertEqual(path.read_bytes(), b"old")




if __name__ == "__main__":
    unittest.main()
