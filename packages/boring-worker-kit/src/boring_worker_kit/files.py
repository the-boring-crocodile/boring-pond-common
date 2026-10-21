"""Worker-owned, policy-scoped file operations."""
from __future__ import annotations

import base64
import binascii
import asyncio
import concurrent.futures
import errno
import fcntl
import os
import stat
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, cast

from boring_file_core import (RESERVED_PREFIXES, InvalidPath, NotAllowedFile, NotAllowedMetadata, OutsideScope, StaleEdit, TooLarge,
                              atomic_write, digest, directory_fd, edit_metadata, parts, read_regular, root_fd,
                              verify_target)
from boring_file_core.textformat import (TextFormatError, decode, encode, target_format)
from boring_file_core.textpatch import PatchError, apply_edits
from .file_policy import FilePolicy, FileRoot
from .batch_contracts import MAX_SEARCH_MATCHES, admit_single
from .observation import Observation
from .outcomes import Refusal
from .inputs import integer, table, text

_WRITES = frozenset({"file_write", "file_patch", "file_write_bytes"})


def _binary(value: object) -> bytes:
    if not isinstance(value, str) or len(value) > 87384:
        raise Refusal("limit", "base64 chunk exceeds input limit")
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise Refusal("invalid_request", "data must be valid base64") from None
    if len(data) > 65536:
        raise Refusal("limit", "byte chunk exceeds 65536 bytes")
    return data


_HASH = frozenset("0123456789abcdef")


def _hash(value: object, *, create: bool = False) -> str | None:
    if value is None and create:
        return None
    if not isinstance(value, str) or len(value) != 64 or any(c not in _HASH for c in value):
        raise Refusal("invalid_request", "expected_sha256 must be a lowercase SHA-256 or null for creation")
    return value


# The .inputs checks raise ValueError only for the value they are given:
# applied to the caller's own input, that is the caller's mistake.
def _text(value: object, name: str) -> str:
    try:
        return text(value, name, empty=True)
    except ValueError as error:
        raise Refusal("invalid_request", str(error)) from None


def _integer(value: object, name: str, minimum: int, maximum: int | None = None) -> int:
    try:
        return integer(value, name, minimum, maximum)
    except ValueError as error:
        raise Refusal("invalid_request", str(error)) from None


@contextmanager
def _directory(root: int, components: tuple[str, ...]) -> Iterator[int]:
    """Open a directory the caller named; its absence is an answer, not a failure."""
    with ExitStack() as stack:
        try:
            fd = stack.enter_context(directory_fd(root, components))
        except FileNotFoundError:
            raise Refusal("not_found", "directory does not exist") from None
        except NotADirectoryError:
            raise Refusal("invalid_request", "a path component is not a directory") from None
        except PermissionError:
            raise Refusal("forbidden", "directory is not accessible") from None
        except OSError as error:
            if error.errno != errno.ELOOP:
                raise
            raise Refusal("forbidden", "a path component is a symbolic link") from None
        yield fd


def _read(parent: int, name: str, maximum: int, *, single_link: bool = False) -> tuple[bytes, os.stat_result]:
    """Read a file the caller named, mapping the core's typed outcomes by type."""
    try:
        return read_regular(parent, name, maximum, single_link=single_link)
    except NotAllowedFile as error:
        raise Refusal("forbidden", str(error)) from None
    except TooLarge as error:
        raise Refusal("limit", str(error)) from None
    except PermissionError:
        raise Refusal("forbidden", "file is not accessible") from None
    except OSError as error:
        if error.errno != errno.ELOOP:  # O_NOFOLLOW: the file is a symbolic link
            raise
        raise Refusal("forbidden", "file is not an allowed regular file") from None


def _current_sha256(parent: int, name: str, maximum: int) -> str | None:
    """The target's hash now, for a stale refusal; None when it is absent or
    not a readable plain file (the refusal stays `stale` either way)."""
    try:
        return digest(_read(parent, name, maximum)[0])
    except (Refusal, OSError):
        return None


def _representable(name: str) -> bool:
    """A name that is not UTF-8 cannot be returned as JSON or addressed by path."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


@contextmanager
def _target_lock(lock_dir: Path, parent: int, name: str) -> Iterator[None]:
    # Fixed stripes keep lock state bounded; collisions only serialize extra edits.
    # The opened parent survives path aliases and target replacement. Root labels
    # and target inodes do not identify a stable shared edit destination.
    info = os.fstat(parent)
    key = digest(f"{info.st_dev}:{info.st_ino}:{name}".encode())[:2]
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(lock_dir / key, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Refusal("busy", "file edit lock deadline exceeded")
                time.sleep(.025)
        yield
    finally:
        os.close(fd)


@dataclass
class SearchState:
    files: int = 0
    bytes: int = 0
    entries: int = 0
    skipped: int = 0
    reason: str | None = None


class FileTools:
    def __init__(self, policy: FilePolicy, lock_dir: Path, settings: object | None = None,
                 *, reserved_prefixes: tuple[str, ...] = ()):
        """`reserved_prefixes`: the consumer's own internal names, hidden and refused
        like boring_file_core.RESERVED_PREFIXES (for example Pirates' ".pirate-")."""
        self.policy = policy
        self.reserved = tuple(reserved_prefixes)
        self.settings = settings
        self.lock_dir = lock_dir
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=policy.limits["max_concurrent_operations"])
        self.slots = asyncio.Semaphore(policy.limits["max_concurrent_operations"])

    def prepare(self, operation: str, arguments: dict[str, object]) -> dict[str, object]:
        step = admit_single(operation, arguments)
        args = step.arguments.model_dump(exclude_unset=True)
        _, _, path = self._scope(args["root"], args.get("path", "."),
                                 operation in _WRITES,
                                 operation in ("file_list", "file_search"))
        limits = self.policy.limits
        if operation == "file_write":
            _hash(args["expected_sha256"], create=True)
            if len(args["content"].encode("utf-8")) > limits["max_file_bytes"]:
                raise Refusal("limit", "file exceeds write limit")
        elif operation == "file_write_bytes":
            _hash(args["expected_sha256"], create=True)
            if len(_binary(args["data"])) > limits["max_file_bytes"]:
                raise Refusal("limit", "file exceeds write limit")
            if args["expected_sha256"] is None and args.get("offset") not in (None, 0):
                raise Refusal("invalid_request", "a new file starts at offset 0")
        elif operation == "file_read_bytes":
            if args.get("length", 16384) > max(0, (limits["max_output_bytes"] - 1024) // 4 * 3):
                raise Refusal("limit", "byte range exceeds output budget")
        elif operation == "file_patch":
            _hash(args["expected_sha256"])
            if len(args["edits"]) > limits["max_edits"]:
                raise Refusal("invalid_request", "invalid edit count")
        elif operation == "file_list" and args.get("limit", 100) > limits["max_list_entries"]:
            raise Refusal("limit", "listing limit exceeds policy")
        elif operation == "file_search":
            query = args["query"]
            if not query or "\r" in query or "\n" in query:
                raise Refusal("invalid_request", "query must be nonempty single-line text")
            if len(query.encode("utf-8")) > 1024:
                raise Refusal("limit", "query is too large")
        return args

    async def call(self, operation: str, request: dict[str, object],
                   *, batch_id: str | None = None, step_id: str | None = None) -> dict[str, object]:
        args = {k: v for k, v in request.items() if k not in ("operation", "request_id")}
        try:
            prepared = self.prepare(operation, args)
        except (TextFormatError, UnicodeEncodeError) as error:
            raise Refusal("invalid_request", str(error)) from None
        actual = {"operation": operation, "request_id": request.get("request_id", "direct"), **prepared}
        # A cancelled caller must retain the slot until its thread has stopped.
        try:
            await asyncio.wait_for(self.slots.acquire(), timeout=5)
        except asyncio.TimeoutError:
            raise Refusal("busy", "all file operation slots are in use") from None
        future = asyncio.get_running_loop().run_in_executor(
            self.pool, self._observed_call, operation, actual, batch_id, step_id)
        future.add_done_callback(lambda _: self.slots.release())
        return await asyncio.shield(future)

    def _observed_call(self, operation: str, request: dict[str, object],
                       batch_id: str | None, step_id: str | None) -> dict[str, object]:
        observer = Observation(self.settings, "file:" + operation, "worker") if self.settings else None
        fields: dict[str, object] = {"method": operation,
            "root": str(request["root"])[:63], "path": str(request.get("path", "."))[:256]}
        if batch_id is not None:
            fields.update(batch_id=batch_id, step_id=step_id)
        started = time.monotonic()
        def emit(event: str, **metadata: object) -> None:
            if observer:
                try:
                    observer.emit(event, **metadata)
                except Exception:
                    pass  # Observability cannot change a file outcome.
        if observer:
            emit("file_state", state="started", **fields)
        try:
            result = self._call(operation, request)
            if observer:
                emit("file_state", state="completed", duration_ms=int((time.monotonic()-started)*1000),
                              bytes=result.get("bytes"), edits=result.get("applied_edits"),
                              truncated=result.get("truncated"), **fields)
            return result
        except BaseException:
            if observer:
                emit("file_state", state="failed", duration_ms=int((time.monotonic()-started)*1000), **fields)
            raise
        finally:
            if observer:
                try:
                    observer.close()
                except Exception:
                    pass

    def _scope(self, root: object, path: object, write: bool, directory: bool) -> tuple[str, FileRoot, tuple[str, ...]]:
        if not isinstance(root, str) or root not in self.policy.roots:
            raise Refusal("forbidden", "file root is unavailable")
        policy = self.policy.roots[root]
        if not policy.read or (write and not policy.write):
            raise Refusal("forbidden", "file operation is disabled for this root")
        if not isinstance(path, str):
            raise Refusal("invalid_request", "path must be a string")
        try:
            p = parts(path, directory=directory, reserved=self.reserved)
        except InvalidPath as error:
            raise Refusal("invalid_request", str(error)) from None
        except OutsideScope as error:
            raise Refusal("forbidden", str(error)) from None
        if any(p[:len(denied)] == denied for denied in policy.deny):
            raise Refusal("forbidden", "path is outside file scope")
        return root, policy, p

    def _call(self, operation: str, request: dict[str, object]) -> dict[str, object]:
        try:
            return self._file_call(operation, request)
        except (TextFormatError, UnicodeEncodeError) as error:
            raise Refusal("invalid_request", str(error)) from None

    def _file_call(self, operation: str, request: dict[str, object]) -> dict[str, object]:
        fields = {
            "file_read": ({"root", "path"}, {"start_line", "line_count"}),
            "file_write": ({"root", "path", "content", "expected_sha256"}, {"encoding", "bom", "newline"}),
            "file_patch": ({"root", "path", "expected_sha256", "edits"}, {"newline"}),
            "file_read_bytes": ({"root", "path"}, {"offset", "length"}),
            "file_write_bytes": ({"root", "path", "data", "expected_sha256"}, {"offset"}),
            "file_list": ({"root"}, {"path", "limit"}),
            "file_search": ({"root", "query"}, {"path", "limit"}),
        }
        required, optional = fields[operation]
        args = table(request, operation, required | {"operation", "request_id"}, optional)
        root, policy, p = self._scope(args["root"], args.get("path", "."), operation in _WRITES, operation in ("file_list", "file_search"))
        limits = self.policy.limits
        if operation in ("file_read", "file_read_bytes"):
            start = _integer(args.get("start_line", 1), "start_line", 1)
            count = _integer(args.get("line_count", 1000), "line_count", 1, 10000)
            with root_fd(policy.path) as fd, _directory(fd, p[:-1]) as parent:
                try:
                    data, _ = _read(parent, p[-1], limits["max_file_bytes"])
                except FileNotFoundError:
                    raise Refusal("not_found", "file does not exist") from None
            if operation == "file_read_bytes":
                offset = _integer(args.get("offset", 0), "offset", 0)
                length = _integer(args.get("length", 16384), "length", 1, 65536)
                chunk = data[offset:offset + length]
                return {"data": base64.b64encode(chunk).decode("ascii"),
                        "offset": offset, "length": len(chunk), "bytes": len(data),
                        "eof": offset + len(chunk) >= len(data), "sha256": digest(data)}
            decoded = decode(data)
            value = decoded.text
            raw_lines = value.split("\n")
            lines = [line + "\n" for line in raw_lines[:-1]] + ([raw_lines[-1]] if raw_lines[-1] else [])
            selected = []
            used = 0
            cap = limits["max_output_bytes"] // 6
            for line in lines[start-1:start-1+count]:
                size = len(line.encode("utf-8"))
                if used + size > cap:
                    break
                selected.append(line); used += size
            if not selected and start <= len(lines):
                raise Refusal("limit", "line exceeds output budget")
            next_line = start + len(selected)
            return {"content": "".join(selected), "sha256": digest(data), "bytes": len(data),
                    "start_line": start, "returned_lines": len(selected), "total_lines": len(lines),
                    "next_line": next_line if next_line <= len(lines) else None,
                    "truncated": next_line <= len(lines), **decoded.format.as_dict()}
        if operation in _WRITES:
            expected = _hash(args["expected_sha256"], create=operation in ("file_write", "file_write_bytes"))
            if operation == "file_patch" and expected is None:
                raise Refusal("invalid_request", "patch requires expected hash")
            with root_fd(policy.path) as fd, _directory(fd, p[:-1]) as parent:
                with _target_lock(self.lock_dir, parent, p[-1]):
                    try:
                        old, info = _read(parent, p[-1], limits["max_file_bytes"], single_link=True)
                    except FileNotFoundError:
                        if expected is not None:
                            raise Refusal("stale", "stale edit: file is absent",
                                          current_sha256=None) from None
                        old, info = b"", None
                    if info is not None:
                        if expected != digest(old):
                            raise Refusal("stale", "stale edit: expected hash differs",
                                          current_sha256=digest(old))
                        if info.st_mode & 0o7000 or info.st_uid != os.getuid():
                            raise Refusal("forbidden", "file owner or privileged mode is not editable")
                        try:
                            metadata = edit_metadata(parent, p[-1], info)
                        except NotAllowedMetadata as error:
                            raise Refusal("forbidden", str(error)) from None
                        mode = metadata.mode
                    else:
                        metadata = None
                        mode = None  # Kernel umask/default ACL; no post-create chmod.
                    fmt = None
                    if operation == "file_write_bytes":
                        chunk = _binary(args["data"])
                        write_offset = args.get("offset")
                        if write_offset is None:
                            payload = chunk
                        else:
                            write_offset = _integer(write_offset, "offset", 0)
                            if write_offset > len(old):
                                raise Refusal("invalid_request", "offset is past the end of the file")
                            payload = old[:write_offset] + chunk + old[write_offset + len(chunk):]
                    elif operation == "file_write":
                        existing = decode(old).format if info is not None else None
                        fmt = target_format(existing, cast(str | None, args.get("encoding")),
                                            cast(bool | None, args.get("bom")), cast(str | None, args.get("newline")))
                        value = _text(args["content"], "content")
                        payload = encode(value, fmt)
                    else:
                        edits = args["edits"]
                        if not isinstance(edits, list) or not 1 <= len(edits) <= limits["max_edits"]:
                            raise Refusal("invalid_request", "invalid edit count")
                        decoded = decode(old)
                        pairs = []
                        for edit in edits:
                            e = table(edit, "edit", {"old_text", "new_text"})
                            pairs.append((_text(e["old_text"], "old_text"),
                                          _text(e["new_text"], "new_text")))
                        try:
                            value = apply_edits(decoded.text, pairs)
                        except PatchError as error:
                            raise Refusal("invalid_request", str(error), **error.details) from None
                        fmt = target_format(decoded.format, None, None, cast(str | None, args.get("newline")))
                        payload = encode(value, fmt)
                    if len(payload) > limits["max_file_bytes"]:
                        raise Refusal("limit", "file exceeds write limit")
                    try:
                        verify_target(parent, p[-1], expected, info, limits["max_file_bytes"], metadata)
                        atomic_write(parent, p[-1], payload, mode, create=info is None,
                                     prefix=".boring-file-", group=info.st_gid if info else None,
                                     metadata=metadata,
                                     before_replace=(lambda: verify_target(parent, p[-1], expected, info,
                                                                            limits["max_file_bytes"], metadata))
                                     if info is not None else None)
                    except StaleEdit as error:
                        raise Refusal("stale", str(error), current_sha256=_current_sha256(
                            parent, p[-1], limits["max_file_bytes"])) from None
                    result: dict[str, object] = {"sha256": digest(payload), "bytes": len(payload)}
                    if fmt is not None:
                        result.update(fmt.as_dict())
                    if operation == "file_patch":
                        result["applied_edits"] = len(edits)
                    return result
        limit = _integer(args.get("limit", 100), "limit", 1,
                         limits["max_list_entries"] if operation == "file_list" else MAX_SEARCH_MATCHES)
        with root_fd(policy.path) as fd, _directory(fd, p) as directory:
            if operation == "file_list":
                entries: list[dict[str, object]] = []
                skipped = 0
                with os.scandir(directory) as listing:
                    for entry in listing:
                        child = p + (entry.name,)
                        if not self._visible(policy, child):
                            continue
                        if not _representable(entry.name):
                            skipped += 1; continue
                        if len(entries) >= limit:
                            return {"entries": entries, "truncated": True, "reason": "entry_limit",
                                    "skipped": skipped}
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except FileNotFoundError:  # removed while listing
                            continue
                        kind = "directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "other"
                        entries.append({"name": entry.name, "path": "/".join(child), "type": kind,
                                        "size_bytes": info.st_size if kind == "file" else None,
                                        "mtime_ns": info.st_mtime_ns})
                return {"entries": entries, "truncated": False, "reason": None, "skipped": skipped}
            query = _text(args["query"], "query")
            if not query or "\n" in query or "\r" in query:
                raise Refusal("invalid_request", "query must be nonempty single-line text")
            if len(query.encode("utf-8")) > 1024:
                raise Refusal("limit", "query is too large")
            matches: list[dict[str, object]] = []
            state = SearchState()
            self._search(directory, p, policy, query, limit, 0, matches, state)
            return {"matches": matches, "truncated": state.reason is not None,
                    "reason": state.reason, "files_scanned": state.files,
                    "bytes_scanned": state.bytes, "files_skipped": state.skipped}

    def _visible(self, policy: FileRoot, path: tuple[str, ...]) -> bool:
        hidden = RESERVED_PREFIXES + self.reserved
        return not any(x == ".git" or x.startswith(hidden) for x in path) and not any(path[:len(d)] == d for d in policy.deny)

    def _search(self, fd: int, path: tuple[str, ...], policy: FileRoot, query: str, limit: int, depth: int,
                matches: list[dict[str, object]], state: SearchState) -> None:
        limits = self.policy.limits
        with os.scandir(fd) as listing:
            for entry in listing:
                state.entries += 1
                if state.entries > limits["max_search_entries"]:
                    state.reason = "entry_limit"; return
                child = path + (entry.name,)
                if not self._visible(policy, child):
                    continue
                if not _representable(entry.name):
                    state.skipped += 1; continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:  # removed while searching
                    continue
                if stat.S_ISDIR(info.st_mode):
                    if depth >= limits["max_search_depth"]:
                        state.reason = "depth_limit"; continue
                    with ExitStack() as stack:
                        try:
                            nested = stack.enter_context(directory_fd(fd, (entry.name,)))
                        except OSError:  # replaced (symlink, file) or removed meanwhile
                            state.skipped += 1; continue
                        self._search(nested, child, policy, query, limit, depth+1, matches, state)
                elif stat.S_ISREG(info.st_mode):
                    if state.files >= limits["max_search_files"]:
                        state.reason = "file_limit"; return
                    if info.st_size > limits["max_file_bytes"] or state.bytes + info.st_size > limits["max_search_bytes"]:
                        state.skipped += 1; state.reason = "byte_limit"; continue
                    try:
                        data, _ = read_regular(fd, entry.name, limits["max_file_bytes"])
                        value = decode(data).text
                    except (ValueError, UnicodeError, OSError):
                        state.skipped += 1; continue
                    state.files += 1; state.bytes += len(data)
                    for number, line in enumerate(value.split("\n"), 1):
                        if query in line:
                            if len(matches) >= limit:
                                state.reason = "result_limit"; return
                            matches.append({"path": "/".join(child), "line": number, "snippet": line[:512]})
                else:
                    state.skipped += 1
                if state.reason in ("result_limit", "file_limit", "entry_limit"):
                    return
