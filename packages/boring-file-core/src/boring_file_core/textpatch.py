"""Portable exact-text patch semantics; callers own disk, limits and outcomes."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .textformat import normalize_newlines


class PatchError(ValueError):
    def __init__(self, message: str, **details: Any):
        super().__init__(message)
        self.details = details


def checked_edits(edits: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """Validate and normalize edit inputs without reading a target document."""
    if not edits:
        raise PatchError("edits must not be empty")
    normalized = []
    for index, (old, new) in enumerate(edits):
        old, new = normalize_newlines(old), normalize_newlines(new)
        if not old:
            raise PatchError("old_text must be nonempty", edit=index)
        normalized.append((old, new))
    return normalized


def apply_edits(original: str, edits: Sequence[tuple[str, str]]) -> str:
    """Match every edit against the original, rejecting ambiguity and overlap."""
    spans: list[tuple[int, int, str, int]] = []
    for index, (old, new) in enumerate(checked_edits(edits)):
        start = original.find(old)
        second = original.find(old, start + 1) if start >= 0 else -1
        if start < 0 or second >= 0:
            # Count only for the refusal diagnostic. Advancing one character
            # deliberately includes overlapping matches (e.g. aa in aaa).
            count = 0
            cursor = start
            while cursor >= 0:
                count += 1
                cursor = original.find(old, cursor + 1)
            raise PatchError("edit text must occur exactly once", edit=index, matches=count)
        spans.append((start, start + len(old), new, index))
    spans.sort()
    for (_, end, _, first), (start, _, _, second) in zip(spans, spans[1:]):
        if start < end:
            raise PatchError("overlapping edits", edits=[first, second])
    cursor = 0
    pieces: list[str] = []
    for start, end, new, _ in spans:
        pieces.extend((original[cursor:start], new))
        cursor = end
    pieces.append(original[cursor:])
    return "".join(pieces)
