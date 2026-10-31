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


def _absolute(path: Path | None, label: str) -> None:
    """A configured path is absolute and normalized as written (None: not configured)."""
    import os
    if path is not None and (not path.is_absolute() or os.path.normpath(str(path)) != str(path)):
        raise ValueError(label + " must be a normalized absolute path")


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
        value = table(raw, "execution", {"name", "default_cwd", "allowed_roots", "task_socket_path",
                     "task_unit_prefix", "task_cgroup_parent"}, {"output_limit_bytes", "watch_root"})
        def path(item: object, label: str) -> Path:
            # As written: a relative path is refused here, before resolve() could make it absolute.
            result = Path(text(item, label))
            _absolute(result, label)
            return result
        settings = cls(
            text(value["name"], "execution name"),
            path(value["default_cwd"], "default_cwd").resolve(),
            tuple(path(x, "allowed root").resolve() for x in strings(value["allowed_roots"], "allowed_roots")),
            path(value["task_socket_path"], "task_socket_path"),
            text(value["task_unit_prefix"], "task_unit_prefix"),
            path(value["task_cgroup_parent"], "task_cgroup_parent"),
            integer(value.get("output_limit_bytes", 65536), "output_limit_bytes", 1),
            path(value["watch_root"], "watch_root") if value.get("watch_root") is not None else None)
        validate_execution(settings)
        return settings


def validate_execution(settings: ExecutionSettings) -> None:
    """The rules every execution configuration keeps, whoever parsed it.

    A consumer that reads its own configuration format (the Crocodile's
    [environments.X]) calls this on what it built, so worker-kit owns the rules
    once: a lowercase execution name; normalized absolute paths; at least one
    allowed root with default_cwd inside one; a task unit prefix of
    `<product>-task-<name>`; a task cgroup parent that is a direct
    system.slice child slice; an output limit within the frame limit. Raises
    ValueError naming the first broken rule; parsing (resolving paths,
    defaults) stays the caller's.
    """
    import re
    from .framing import MAX_OUTPUT_BYTES

    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", settings.name):
        raise ValueError("invalid execution identity")
    for root in settings.allowed_roots:
        _absolute(root, "allowed root")
    _absolute(settings.default_cwd, "default_cwd")
    cwd = settings.default_cwd
    if not settings.allowed_roots or not any(cwd == root or root in cwd.parents for root in settings.allowed_roots):
        raise ValueError("default_cwd must be inside an allowed root")
    _absolute(settings.task_socket_path, "task_socket_path")
    if not valid_unit_prefix(settings.task_unit_prefix, settings.name):
        raise ValueError("task prefix must match execution identity")
    parent = settings.task_cgroup_parent
    _absolute(parent, "task_cgroup_parent")
    if parent.parent != Path("/system.slice") or not re.fullmatch(r"system-[A-Za-z0-9_-]+\.slice", parent.name):
        raise ValueError("task cgroup parent must be a direct system.slice child slice")
    if not 1 <= settings.output_limit_bytes <= MAX_OUTPUT_BYTES:
        raise ValueError(f"output_limit_bytes must be 1..{MAX_OUTPUT_BYTES}")
