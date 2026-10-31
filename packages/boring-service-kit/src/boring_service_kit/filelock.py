"""Cooperating local file updates hold a stable lock inode across publication."""
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import threading

# msvcrt locks don't replace a thread lock; stripes bound in-process state.
_STRIPES = [threading.RLock() for _ in range(64)]


@contextmanager
def file_lock(path: Path):
    with _STRIPES[hash(str(path.absolute())) % len(_STRIPES)]:
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError("lock must be a regular single-link file")
            if os.name == "nt":
                import msvcrt
                # Windows permits byte-range locks beyond EOF. Do not initialize byte zero:
                # another process may already hold its lock after our fstat saw an empty file.
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    # Never unlink the lock: waiters must all lock the same inode.
