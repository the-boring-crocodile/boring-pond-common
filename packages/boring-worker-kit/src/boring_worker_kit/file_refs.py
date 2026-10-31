"""File references in service calls, and service answers saved to a file.

Content the caller only moves between a file and a service need not pass
through the caller (an AI client's context: tokens both ways, and a copy it
retypes can change). Two stateless parts, both through the Pond's file tools,
so roots, permissions, symlink rules and limits are those of file_read and
file_write:

- In a service call's arguments, a value that is an object whose only key is
  "$file" is a reference: {"$file": {"root": R, "path": P}} (optionally
  "sha256": the expected hash) is replaced by that file's text, byte for byte
  (UTF-8; a leading byte-order mark is dropped; line ends kept). Nested values
  count; a malformed reference is refused; anything else is passed on.
- save_to {"root", "path", "expected_sha256"} writes the service's answer to
  that file (its text when the answer is one text item, else the answer as
  JSON) and returns where it was saved (`saved_to`) instead of the answer.
  When the save does not succeed after the service ran, the answer comes back
  with `save_to_refused` (a refusal: nothing was written) or `save_to_failed`
  (the write failed or did not finish in time: the file may or may not have
  been written; compare its hash before writing again).

call_service puts them around one service call, within a carried deadline;
the Crocodile's worker and Pirates both use it.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import json
import posixpath
from typing import Any

from collections.abc import Awaitable, Callable, Iterator

from .deadlines import REPLY_MARGIN_SECONDS, left
from .settlement import settled
from .files import FileTools
from .outcomes import Refusal
from .services import CALL_TIMEOUT_SECONDS

MARKER = "$file"
MAX_REFERENCES = 16
MAX_REFERENCE_BYTES = 512 * 1024  # all references of one call together
MAX_INLINE_ANSWER = 1024 * 1024  # an answer that could not be saved and is larger is not returned inline
SAVE_FLOOR_SECONDS = 1.0  # a save never gets less, even when the service used its whole time


def has_references(value: object) -> bool:
    """Whether service arguments hold a file reference anywhere (the same rule
    resolve() applies), so a caller can gate them without reading a file."""
    return next(_references(value), None) is not None


def _reference(value: object) -> dict[str, Any] | None:
    """The {"root", "path", "sha256"?} of a reference, None for any other value."""
    if not isinstance(value, dict) or set(value) != {MARKER}:
        return None
    target = value[MARKER]
    if (not isinstance(target, dict) or not {"root", "path"} <= set(target) <= {"root", "path", "sha256"}
            or not all(isinstance(target[k], str) for k in target)):
        raise Refusal("invalid_request", 'a file reference is {"$file": {"root": ..., "path": ..., "sha256": optional}}',
                      fields=["arguments.$file"])
    return target


def _references(value: object, path: tuple[str | int, ...] = ()
                ) -> Iterator[tuple[tuple[str | int, ...], dict[str, Any]]]:
    """The one structural traversal for discovery, admission and resolution."""
    target = _reference(value)
    if target is not None:
        yield path, target
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _references(item, path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _references(item, path + (index,))


def _targets(arguments: object) -> list[tuple[tuple[str | int, ...], dict[str, Any]]]:
    targets = []
    for found in _references(arguments):
        if len(targets) >= MAX_REFERENCES:
            raise Refusal("limit", f"at most {MAX_REFERENCES} file references in one call")
        targets.append(found)
    return targets


def prepare_call(files: FileTools, arguments: object, save_to: object = None) -> None:
    """Non-reading admission for a service call or a whole batch.

    Checks marker shapes/counts and configured read/write scope through FileTools.
    Files/directories may be created by earlier steps: existence, contents, sizes
    and current hashes are deliberately checked later by call_service/resolve.
    """
    if arguments is not None and (not isinstance(arguments, dict) or _reference(arguments) is not None):
        raise Refusal("invalid_request", "arguments must be an object; put the file reference inside it",
                      fields=["arguments"])
    for _, target in _targets(arguments):
        files.prepare("file_read_bytes", {"root": target["root"], "path": target["path"], "length": 1})
    target = check_save_to(save_to)
    if target is not None:
        _prepare_target(files, target)


def _prepare_target(files: FileTools, target: dict[str, Any]) -> None:
    files.prepare("file_write", {"root": target["root"], "path": target["path"], "content": "",
                                 "expected_sha256": target["expected_sha256"]})


async def _read(files: FileTools, target: dict[str, Any], budget: int) -> tuple[str, int]:
    """The exact text of one referenced file, read as bytes in parts (no line or
    line-end rules); refused when it changes between parts, differs from the
    expected hash, exceeds the budget, or is not UTF-8."""
    part = max(1, min(65536, (files.policy.limits["max_output_bytes"] - 1024) // 4 * 3))
    data, offset, sha = bytearray(), 0, None
    while True:
        result = await files.call("file_read_bytes", {"operation": "file_read_bytes", "request_id": "file-ref",
                                                      "root": target["root"], "path": target["path"],
                                                      "offset": offset, "length": part})
        chunk, size, now = result.get("data"), result.get("bytes"), result.get("sha256")
        if not isinstance(chunk, str) or not isinstance(size, int) or not isinstance(now, str):
            raise RuntimeError("file_read_bytes returned an unexpected shape")
        if sha is None:
            sha = now
            if "sha256" in target and target["sha256"] != sha:
                raise Refusal("stale", "file reference: the file's sha256 is not the expected one",
                              current_sha256=sha)
            if size > budget:
                raise Refusal("limit", f"file references exceed {MAX_REFERENCE_BYTES} bytes in one call")
        elif now != sha:
            raise Refusal("stale", "file reference: the file changed while it was read", current_sha256=now)
        piece = base64.b64decode(chunk)
        data += piece
        offset += len(piece)
        if result.get("eof") or not piece:
            break
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        raise Refusal("invalid_request", "file reference: the file is not UTF-8 text") from None
    return text.removeprefix("\ufeff"), len(data)


async def resolve(arguments: object, files: FileTools) -> tuple[object, list[dict[str, object]]]:
    """The arguments with every reference replaced by its file's text, and what was read.

    Refused (nothing is sent): a malformed reference (`invalid_request`), more
    than MAX_REFERENCES or MAX_REFERENCE_BYTES in all (`limit`), a hash that is
    not the expected one or a file that changes while read (`stale`), a file
    that is not UTF-8 (`invalid_request`), and every refusal of file_read_bytes
    itself (`not_found`, `forbidden`, ...)."""
    used: list[dict[str, object]] = []
    targets = _targets(arguments)
    resolved: Any = copy.deepcopy(arguments)
    budget = MAX_REFERENCE_BYTES
    for path, target in targets:
        text, size = await _read(files, target, budget)
        budget -= size
        used.append({"root": target["root"], "path": target["path"], "bytes": size})
        if not path:
            resolved = text
        else:
            parent = resolved
            for key in path[:-1]:
                parent = parent[key]
            parent[path[-1]] = text
    return resolved, used


def check_save_to(save_to: object) -> dict[str, Any] | None:
    """save_to as given, checked before the call runs (None: not asked for)."""
    if save_to is None:
        return None
    if (not isinstance(save_to, dict) or set(save_to) != {"root", "path", "expected_sha256"}
            or not isinstance(save_to["root"], str) or not isinstance(save_to["path"], str)
            or not (save_to["expected_sha256"] is None or isinstance(save_to["expected_sha256"], str))):
        raise Refusal("invalid_request", "save_to takes root, path and expected_sha256 "
                      "(null for a new file, or the sha256 of the file to replace)", fields=["save_to"])
    return save_to


async def check_target(files: FileTools, save_to: dict[str, Any]) -> None:
    """Before the call: the save target must match expected_sha256 (absent for null),
    so a service that ran does not lose its answer to a stale target; and the root
    must allow writing there (the file tools' own scope check, nothing written)."""
    _prepare_target(files, save_to)
    try:
        current = await files.call("file_read", {"operation": "file_read", "request_id": "save-to",
                                                 "root": save_to["root"], "path": save_to["path"],
                                                 "start_line": 1, "line_count": 1})
    except Refusal as refusal:
        if refusal.code != "not_found":
            raise
        if save_to["expected_sha256"] is not None:
            raise Refusal("stale", "save_to: the file is absent; use expected_sha256 null to create it",
                          current_sha256=None) from None
        # Absent file, or absent directory: file_write creates no directories.
        parent = posixpath.dirname(save_to["path"].rstrip("/")) or "."
        try:
            await files.call("file_list", {"operation": "file_list", "request_id": "save-to",
                                           "root": save_to["root"], "path": parent, "limit": 1})
        except Refusal as missing:
            if missing.code != "not_found":
                raise
            raise Refusal("not_found", f"save_to: the directory {parent} does not exist (file_write creates "
                          "no directories)") from None
        return
    if save_to["expected_sha256"] != current["sha256"]:
        raise Refusal("stale", "save_to: the file exists with another sha256"
                      + ("; read it, or choose a new path" if save_to["expected_sha256"] is None else ""),
                      current_sha256=current["sha256"])


def answer_text(answer: dict[str, object]) -> str:
    """The text a saved answer becomes: one text item as itself, anything else as JSON."""
    content = answer.get("content")
    if (isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict)
            and content[0].get("type") == "text" and isinstance(content[0].get("text"), str)
            and "structuredContent" not in answer):
        return str(content[0]["text"])
    return json.dumps(answer, ensure_ascii=False, indent=2) + "\n"


def _receipt(save_to: dict[str, Any], answer: dict[str, object], written: dict[str, object],
             references: list[dict[str, object]]) -> dict[str, object]:
    content = answer.get("content")
    receipt: dict[str, object] = {
        "saved_to": {"root": save_to["root"], "path": save_to["path"], **written},
        "content_types": [item.get("type") for item in content if isinstance(item, dict)]
        if isinstance(content, list) else []}
    if references:
        receipt["file_references"] = references
    return receipt


async def save(files: FileTools, save_to: dict[str, Any], answer: dict[str, object],
               references: list[dict[str, object]], timeout: float | None = None, *,
               max_inline_answer: int | None = None, settle: bool = False,
               on_late_save: Callable[[dict[str, object]], None] | None = None) -> dict[str, object]:
    """Write the answer to save_to; `saved_to` replaces the answer. The service has
    run, so its answer is never dropped here:

    - the file tools refuse the write: the answer with `save_to_refused` (the
      refusal, with its code; nothing was written);
    - the write fails, or does not finish within `timeout`: the answer with
      `save_to_failed` ({"message", "effects": "unknown"}: the file may or may not
      be written). A write still running then goes on (the file tools own it)
      and its record goes to `on_late_save` with "landed_after": "timeout";
    - the caller is cancelled: the local write is waited for up to
      REPLY_MARGIN_SECONDS (it is short and owned by the file tools); its record
      goes to `on_late_save` with "landed_after": "cancellation" when it landed
      by then, or with "timeout" when it lands later. The cancellation stands.

    `settle=True` is for a caller that owns the whole lifecycle and settles its
    own file tool calls (a Pirate assignment): the save is a file write like any
    other, waited for without `timeout`, and a cancellation is deferred until it
    settles (its record then goes to `on_late_save`, "landed_after":
    "cancellation").

    An answer larger than `max_inline_answer` (the caller's own result limit;
    None: MAX_INLINE_ANSWER) comes back as its size with `service_ran`."""
    write = asyncio.ensure_future(files.call("file_write", {
        "operation": "file_write", "request_id": "save-to", "root": save_to["root"], "path": save_to["path"],
        "content": answer_text(answer), "expected_sha256": save_to["expected_sha256"]}))

    def late(landed_after: str) -> Callable[[asyncio.Future[dict[str, object]]], None]:
        def report(task: asyncio.Future[dict[str, object]]) -> None:
            if task.cancelled() or task.exception() is not None:  # retrieved, so never "unretrieved"
                return
            if on_late_save is not None:
                on_late_save({**_receipt(save_to, answer, task.result(), references), "landed_after": landed_after})
        return report
    problem: dict[str, object] = {}
    try:
        if settle:
            written = await settled(write, lambda result: late("cancellation")(write))
        else:
            written = await asyncio.wait_for(asyncio.shield(write), timeout)
    except asyncio.TimeoutError:
        write.add_done_callback(late("timeout"))
        problem = {"save_to_failed": {"message": "the write did not finish within this request's time; "
                                      "the file may still be written", "effects": "unknown"}}
    except asyncio.CancelledError:
        if not settle:
            # Bounded: a hung file system must not hold a cancelled caller forever.
            await asyncio.wait([write], timeout=REPLY_MARGIN_SECONDS)
            if write.done():
                late("cancellation")(write)
            else:
                write.add_done_callback(late("timeout"))
        raise
    except Refusal as refusal:
        problem = {"save_to_refused": refusal.as_data()}
    except Exception as error:  # an OS error (disk full, permissions) or a bug: the service ran
        problem = {"save_to_failed": {"message": f"{type(error).__name__}: {error}"[:512], "effects": "unknown"}}
    if problem:
        size = len(json.dumps(answer, ensure_ascii=True).encode())
        if size > (MAX_INLINE_ANSWER if max_inline_answer is None else max_inline_answer):
            return {**problem, "service_ran": True, "answer_bytes": size,
                    "note": "the service answered, but its answer is too large to return here"}
        return {**answer, **problem}
    return _receipt(save_to, answer, written, references)


async def call_service(files: FileTools, arguments: object, save_to: object,
                       call: Callable[[object, float], Awaitable[dict[str, object]]],
                       deadline: float | None = None, *, max_inline_answer: int | None = None,
                       settle_save: bool = False,
                       on_late_save: Callable[[dict[str, object]], None] | None = None) -> dict[str, object]:
    """One service call with file references and save_to, within `deadline` (event-loop
    time; None: the service contract only). `call(arguments, timeout)` makes the call
    itself; the service gets what is left, at most CALL_TIMEOUT_SECONDS, minus
    REPLY_MARGIN_SECONDS kept back for save_to.

    Refused before anything is sent: a malformed save_to, a stale, unwritable or
    directory-less target, a reference refusal (see resolve), `arguments` that is
    itself a reference (it must be an object), or no time left (`limit`; the
    checks and reads before the call count against the deadline). Whatever `call`
    raises once it has sent (a timeout, a broken connection, the service's own
    failure) passes through unchanged: the call may have run. Cancellation during
    the call cancels it (may have run); during the save, see save(). The save gets
    what is left, at least SAVE_FLOOR_SECONDS (`settle_save`: see save())."""
    prepare_call(files, arguments, save_to)
    target = check_save_to(save_to)

    async def prepare() -> tuple[object, list[dict[str, object]]]:
        if target is not None:
            await check_target(files, target)
        return await resolve({} if arguments is None else arguments, files)
    before = left(deadline)
    if before is not None and before <= 0:
        raise Refusal("limit", "no time left for the service call in this request; nothing was sent")
    try:
        resolved, references = await (prepare() if before is None else asyncio.wait_for(prepare(), before))
    except asyncio.TimeoutError:
        raise Refusal("limit", "file references and the save_to check did not finish within this "
                      "request's time; nothing was sent") from None
    remaining = left(deadline)
    timeout = CALL_TIMEOUT_SECONDS if remaining is None else min(
        CALL_TIMEOUT_SECONDS, remaining - (REPLY_MARGIN_SECONDS if target is not None else 0.0))
    if timeout <= 0:
        raise Refusal("limit", "no time left for the service call in this request; nothing was sent")
    answer = await call(resolved, timeout)
    if target is None:
        return answer
    after = left(deadline)
    return await save(files, target, answer, references, None if after is None else max(after, SAVE_FLOOR_SECONDS),
                      max_inline_answer=max_inline_answer, settle=settle_save, on_late_save=on_late_save)
