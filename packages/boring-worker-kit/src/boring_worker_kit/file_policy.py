"""Explicit file authorization and limits; no command policy inheritance."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from boring_file_core import parts
from .inputs import integer, table, text

_ID = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")
_LIMITS = {
    "max_file_bytes": (524288, 1048576),
    "max_output_bytes": (98304, 131072),
    "max_edits": (32, 128),
    "max_list_entries": (100, 500),
    "max_search_depth": (8, 32),
    "max_search_files": (256, 2048),
    "max_search_entries": (2048, 16384),
    "max_search_bytes": (1048576, 8388608),
    "max_concurrent_operations": (4, 16),
}


@dataclass(frozen=True)
class FileRoot:
    path: Path
    read: bool
    write: bool
    deny: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class FilePolicy:
    roots: dict[str, FileRoot]
    limits: dict[str, int]

    @classmethod
    def parse(cls, value: object) -> "FilePolicy":
        if value is None:
            return cls({}, {k: default for k, (default, _) in _LIMITS.items()})
        config = table(value, "files", set(), {"roots", "limits"})
        raw_roots = table(config.get("roots", {}), "files.roots")
        limits = table(config.get("limits", {}), "files.limits", set(), set(_LIMITS))
        parsed_limits = {k: integer(limits.get(k, default), k, 1, maximum)
                         for k, (default, maximum) in _LIMITS.items()}
        roots: dict[str, FileRoot] = {}
        for name, raw in raw_roots.items():
            if not _ID.fullmatch(name):
                raise ValueError("invalid file root ID")
            entry = table(raw, "file root", {"path", "read", "write"}, {"deny"})
            raw_path = text(entry["path"], "file root path")
            path = Path(raw_path)
            if not path.is_absolute() or os.path.normpath(raw_path) != raw_path:
                raise ValueError("file root must be a normalized absolute path")
            if type(entry["read"]) is not bool or type(entry["write"]) is not bool:
                raise ValueError("file permissions must be booleans")
            if entry["write"] and not entry["read"]:
                raise ValueError("file write requires read")
            denied = entry.get("deny", [])
            if not isinstance(denied, list) or any(not isinstance(x, str) for x in denied):
                raise ValueError("file deny must be a list of relative subtrees")
            roots[name] = FileRoot(path, entry["read"], entry["write"], tuple(parts(x) for x in denied))
        paths = [root.path for root in roots.values()]
        if any(a == b or a in b.parents or b in a.parents for i, a in enumerate(paths) for b in paths[i+1:]):
            raise ValueError("file roots must not overlap")
        return cls(roots, parsed_limits)

    def public(self) -> dict[str, object]:
        return {"configured": bool(self.roots), "availability_verified": False,
                "roots": {name: {"read": r.read, "write": r.write} for name, r in self.roots.items()},
                "limits": self.limits.copy()}
