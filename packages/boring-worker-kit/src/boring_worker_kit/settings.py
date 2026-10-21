"""Structural execution configuration; callers own their site configuration."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

def valid_unit_prefix(prefix: str, name: str) -> bool:
    """A task unit prefix is `<product>-task-<execution name>`.

    The product part is the consumer's own (for example boring-crocodile or
    boring-pirate); the execution name must be the environment's, so a unit
    can only serve the identity it is named after.
    """
    import re
    return re.fullmatch(r"[a-z][a-z0-9-]{0,40}-task-" + re.escape(name), prefix) is not None


class ExecutionSettings(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def default_cwd(self) -> Path: ...
    @property
    def allowed_roots(self) -> tuple[Path, ...]: ...
    @property
    def task_socket_path(self) -> Path | None: ...
    @property
    def task_unit_prefix(self) -> str: ...
    @property
    def task_cgroup_parent(self) -> Path: ...
    @property
    def output_limit_bytes(self) -> int: ...

@dataclass(frozen=True)
class EnvironmentSettings:
    name: str
    default_cwd: Path
    allowed_roots: tuple[Path, ...]
    task_socket_path: Path | None
    task_unit_prefix: str
    task_cgroup_parent: Path
    output_limit_bytes: int = 65536
    # Root-owned directory of admin watch sockets; None: no observation.
    watch_root: Path | None = None

    @classmethod
    def from_mapping(cls, raw: object) -> "EnvironmentSettings":
        from .inputs import table, text, integer, strings
        from .framing import MAX_OUTPUT_BYTES
        import os
        import re
        value = table(raw, "execution", {"name", "default_cwd", "allowed_roots", "task_socket_path",
                     "task_unit_prefix", "task_cgroup_parent"}, {"output_limit_bytes", "watch_root"})
        def path(value: object, label: str) -> Path:
            result = Path(text(value, label))
            if not result.is_absolute() or os.path.normpath(str(result)) != str(result):
                raise ValueError(label + " must be a normalized absolute path")
            return result
        name = text(value["name"], "execution name")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", name):
            raise ValueError("invalid execution identity")
        roots = tuple(path(x, "allowed root").resolve() for x in strings(value["allowed_roots"], "allowed_roots"))
        cwd = path(value["default_cwd"], "default_cwd").resolve()
        if not roots or not any(cwd == root or root in cwd.parents for root in roots):
            raise ValueError("default_cwd must be inside an allowed root")
        prefix = text(value["task_unit_prefix"], "task_unit_prefix")
        if not valid_unit_prefix(prefix, name):
            raise ValueError("task prefix must match execution identity")
        parent = path(value["task_cgroup_parent"], "task_cgroup_parent")
        if parent.parent != Path("/system.slice") or not re.fullmatch(r"system-[A-Za-z0-9_-]+\.slice", parent.name):
            raise ValueError("task cgroup parent must be a direct system.slice child slice")
        return cls(name, cwd, roots, path(value["task_socket_path"], "task_socket_path"), prefix, parent,
                   integer(value.get("output_limit_bytes", 65536), "output_limit_bytes", 1, MAX_OUTPUT_BYTES),
                   path(value["watch_root"], "watch_root") if value.get("watch_root") is not None else None)
