"""The execution rules are worker-kit's, whoever parses the configuration."""
from dataclasses import dataclass, replace
from pathlib import Path
import tempfile
import unittest

from boring_worker_kit.framing import MAX_OUTPUT_BYTES
from boring_worker_kit.settings import EnvironmentSettings, validate_execution


@dataclass(frozen=True)
class ConsumerSettings:
    """A consumer's own settings type (more fields, its own parser)."""
    name: str
    display_name: str
    default_cwd: Path
    allowed_roots: tuple[Path, ...]
    task_socket_path: Path | None
    task_unit_prefix: str
    task_cgroup_parent: Path
    output_limit_bytes: int


class ValidateExecutionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.good = ConsumerSettings("build", "Build", self.root / "work", (self.root,), None,
                                     "acme-task-build", Path("/system.slice/system-acme.slice"), 1024)

    def tearDown(self):
        self.temp.cleanup()

    def test_a_consumer_type_is_checked_by_the_same_rules(self):
        validate_execution(self.good)
        for change, message in [
                (dict(name="Build"), "invalid execution identity"),
                (dict(default_cwd=Path("/elsewhere")), "default_cwd must be inside an allowed root"),
                (dict(allowed_roots=()), "default_cwd must be inside an allowed root"),
                (dict(default_cwd=Path("work")), "default_cwd must be a normalized absolute path"),
                (dict(task_socket_path=Path("/run/x/../y.sock")), "task_socket_path must be a normalized"),
                (dict(task_unit_prefix="acme-task-other"), "task prefix must match execution identity"),
                (dict(task_cgroup_parent=Path("/acme.slice")), "direct system.slice child slice"),
                (dict(output_limit_bytes=MAX_OUTPUT_BYTES + 1), "output_limit_bytes must be"),
                (dict(output_limit_bytes=0), "output_limit_bytes must be")]:
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, message):
                validate_execution(replace(self.good, **change))

    def test_from_mapping_refuses_a_relative_path_before_resolving_it(self):
        raw = {"name": "build", "default_cwd": str(self.root), "allowed_roots": [str(self.root)],
               "task_socket_path": "/run/acme/build.sock", "task_unit_prefix": "acme-task-build",
               "task_cgroup_parent": "/system.slice/system-acme.slice"}
        self.assertEqual(EnvironmentSettings.from_mapping(raw).default_cwd, self.root)
        for key, value in [("default_cwd", "."), ("allowed_roots", ["relative"]), ("watch_root", "run/w")]:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "normalized absolute path"):
                EnvironmentSettings.from_mapping({**raw, key: value})
        with self.assertRaisesRegex(ValueError, "task prefix must match"):
            EnvironmentSettings.from_mapping({**raw, "task_unit_prefix": "acme-task-pond"})


if __name__ == "__main__":
    unittest.main()
