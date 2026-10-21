"""Small descriptor-relative filesystem primitives; policy belongs to callers."""
from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, NamedTuple

def _directory_flags() -> int:
    # Evaluated only for Linux disk operations; textformat also imports on Windows.
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_NAME_MAX = 255  # bytes per component on Linux filesystems
# The target is no longer the plain file that was read: for verify_target a
# change, not a reason to fail.
_REPLACED = (errno.ELOOP, errno.EISDIR, errno.ENXIO, errno.ENOTDIR)


# Typed outcomes, so callers decide by type and never by message text. Each is
# a ValueError, as the plain ValueError raised here before was.
class InvalidPath(ValueError):
    """The path is not a well-formed relative path."""


class OutsideScope(ValueError):
    """The path names a reserved or traversing component (.., .git, ...)."""


class NotAllowedFile(ValueError):
    """The target is not a plain single regular file."""


class TooLarge(ValueError):
    """The file exceeds the caller's byte limit."""


class StaleEdit(ValueError):
    """The target changed between the caller's check and the write."""


class NotAllowedMetadata(ValueError):
    """The file has metadata that this replacement cannot preserve."""


class FileMetadata(NamedTuple):
    uid: int
    gid: int
    mode: int
    access_acl: bytes | None


_ACCESS_ACL = "system.posix_acl_access"


def edit_metadata(parent: int, name: str, previous: os.stat_result) -> FileMetadata:
    """Inspect one verified inode; only a POSIX access ACL is understood. Linux only."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or (
            before.st_dev, before.st_ino) != (previous.st_dev, previous.st_ino):
            raise StaleEdit("stale edit: file was replaced")
        if (before.st_uid, before.st_gid, before.st_mode, before.st_ctime_ns) != (
                previous.st_uid, previous.st_gid, previous.st_mode, previous.st_ctime_ns):
            raise StaleEdit("stale edit: file metadata changed")
        names = set(os.listxattr(fd))
        if names - {_ACCESS_ACL}:
            raise NotAllowedMetadata("files with unsupported extended metadata are not editable")
        acl = os.getxattr(fd, _ACCESS_ACL) if _ACCESS_ACL in names else None
        after = os.fstat(fd)
        if (before.st_uid, before.st_gid, before.st_mode, before.st_ctime_ns) != (
                after.st_uid, after.st_gid, after.st_mode, after.st_ctime_ns):
            raise StaleEdit("stale edit: file metadata changed")
        return FileMetadata(after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode), acl)
    finally:
        os.close(fd)


# Names this library itself writes (temporary files of atomic writes).
RESERVED_PREFIXES = (".boring-file-",)


def parts(path: str, *, directory: bool = False, reserved: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Split a relative path; refuse traversal, .git and reserved name prefixes.

    `reserved` adds a consumer's own internal prefixes to RESERVED_PREFIXES.
    """
    if directory and path == ".":
        return ()
    if not isinstance(path, str) or not path or path.startswith("/"):
        raise InvalidPath("invalid relative path")
    try:
        encoded = path.encode("utf-8")
    except UnicodeEncodeError:  # lone surrogates: not a name any file can have
        raise InvalidPath("invalid relative path") from None
    if len(encoded) > 1024:
        raise InvalidPath("invalid relative path")
    result = tuple(path.split("/"))
    if any(len(p.encode("utf-8")) > _NAME_MAX for p in result):
        raise InvalidPath("path component is too long")
    hidden = RESERVED_PREFIXES + tuple(reserved)
    if any(p in ("", ".", "..", ".git") or p.startswith(hidden) or "\x00" in p for p in result):
        raise OutsideScope("path outside editable scope")
    return result


@contextmanager
def root_fd(root: Path) -> Iterator[int]:
    fd = os.open(root, _directory_flags())
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def directory_fd(root: int, components: tuple[str, ...]) -> Iterator[int]:
    fd = os.dup(root)
    try:
        for component in components:
            other = os.open(component, _directory_flags(), dir_fd=fd)
            os.close(fd)
            fd = other
        yield fd
    finally:
        os.close(fd)


def read_regular(parent: int, name: str, maximum: int, *, single_link: bool = False) -> tuple[bytes, os.stat_result]:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
    except OSError as error:
        if error.errno == errno.ENXIO:  # a socket or a device without a reader
            raise NotAllowedFile("file is not an allowed regular file") from None
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (single_link and info.st_nlink != 1):
            raise NotAllowedFile("file is not an allowed regular file")
        if info.st_size > maximum:
            raise TooLarge("file exceeds read limit")
        chunks = bytearray()
        while len(chunks) <= maximum:
            part = os.read(fd, min(65536, maximum + 1 - len(chunks)))
            if not part:
                break
            chunks.extend(part)
        if len(chunks) > maximum:
            raise TooLarge("file exceeds read limit")
        return bytes(chunks), info
    finally:
        os.close(fd)


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def verify_target(parent: int, name: str, expected: str | None, previous: os.stat_result | None, maximum: int, metadata: FileMetadata | None = None) -> None:
    try:
        actual, info = read_regular(parent, name, maximum, single_link=True)
    except FileNotFoundError:
        if previous is not None:
            raise StaleEdit("stale edit: file disappeared") from None
        return
    except (NotAllowedFile, TooLarge):
        # Something else now stands at the name (a FIFO, a second link, a
        # bigger file): the target changed since it was read.
        raise StaleEdit("stale edit: file was replaced") from None
    except OSError as error:
        if error.errno in _REPLACED:  # a symlink or directory took its place
            raise StaleEdit("stale edit: file was replaced") from None
        raise
    if previous is None or (info.st_dev, info.st_ino) != (previous.st_dev, previous.st_ino) or digest(actual) != expected:
        raise StaleEdit("stale edit: file changed")
    if metadata is not None:
        try:
            current = edit_metadata(parent, name, info)
        except NotAllowedMetadata:
            raise StaleEdit("stale edit: file metadata changed") from None
        if current != metadata:
            raise StaleEdit("stale edit: file metadata changed")


def _rename_noreplace(parent: int, temporary: str, name: str) -> None:
    # Linux renameat2(RENAME_NOREPLACE) preserves an externally created target.
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError:  # C library without renameat2 (glibc < 2.28)
        raise OSError(errno.ENOSYS, "renameat2 is not available; cannot create without replacing") from None
    result = renameat2(parent, temporary.encode(), parent, name.encode(), 1)
    if result != 0:
        error = ctypes.get_errno()
        if error == 17:
            raise StaleEdit("stale edit: file appeared")
        raise OSError(error, os.strerror(error))


def atomic_write(parent: int, name: str, payload: bytes, mode: int | None, *, create: bool, prefix: str, group: int | None = None, metadata: FileMetadata | None = None,
                 before_replace: Callable[[], None] | None = None) -> None:
    # None is opt-in normal creation: the kernel applies umask/default ACL.
    # Never use it for replacement, where exact existing metadata is required.
    if mode is None and (not create or metadata is not None or group is not None):
        raise ValueError("inherited creation mode requires a new file without replacement metadata")
    temporary = prefix + secrets.token_hex(16)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o666 if mode is None else mode, dir_fd=parent)
    try:
        try:
            if group is not None:
                os.fchown(fd, -1, group)
            if mode is not None:
                os.fchmod(fd, mode)
            if metadata is not None:
                # A default directory ACL may already be present on the temporary.
                # Match the original access ACL exactly, including its mask.
                if metadata.access_acl is None:
                    if _ACCESS_ACL in os.listxattr(fd):
                        os.removexattr(fd, _ACCESS_ACL)
                else:
                    os.setxattr(fd, _ACCESS_ACL, metadata.access_acl)
                if stat.S_IMODE(os.fstat(fd).st_mode) != metadata.mode:
                    raise OSError("preserved ACL changed the original effective mode")
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(payload)
                stream.flush()
            os.fsync(fd)
        finally:
            os.close(fd)
        if create:
            _rename_noreplace(parent, temporary, name)
        else:
            try:
                if before_replace is not None:
                    before_replace()
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            except (IsADirectoryError, NotADirectoryError):
                raise StaleEdit("stale edit: a directory took the file's place") from None
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
