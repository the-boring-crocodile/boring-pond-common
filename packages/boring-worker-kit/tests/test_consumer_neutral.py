"""Common names no consumer: prefixes, reserved names and the watch root are configuration."""
import tempfile
import unittest
from pathlib import Path

from boring_worker_kit.file_policy import FilePolicy
from boring_worker_kit.files import FileTools
from boring_worker_kit.observation import Observation, watch_path
from boring_worker_kit.outcomes import Refusal
from boring_worker_kit.settings import EnvironmentSettings, valid_unit_prefix


class ConsumerNeutralTest(unittest.TestCase):
    def test_any_product_prefix_bound_to_the_execution_name(self):
        self.assertTrue(valid_unit_prefix("acme-task-build", "build"))
        self.assertTrue(valid_unit_prefix("boring-pirate-task-parrot", "parrot"))
        self.assertFalse(valid_unit_prefix("acme-task-other", "build"))
        self.assertFalse(valid_unit_prefix("acme-task-build-x", "build"))
        self.assertFalse(valid_unit_prefix("Acme-task-build", "build"))

    def test_from_mapping_accepts_an_outside_prefix_and_optional_watch_root(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = {"name": "build", "default_cwd": directory, "allowed_roots": [directory],
                   "task_socket_path": "/run/acme/build.sock", "task_unit_prefix": "acme-task-build",
                   "task_cgroup_parent": "/system.slice/system-acme.slice"}
            settings = EnvironmentSettings.from_mapping(raw)
            self.assertIsNone(settings.watch_root)
            self.assertIsNone(watch_path(settings))
            Observation(settings, "task", "worker").emit("event")  # no watch root: silently off
            watched = EnvironmentSettings.from_mapping({**raw, "watch_root": "/run/acme-watch"})
            self.assertEqual(watch_path(watched), Path("/run/acme-watch/acme-task-build.sock"))

    def test_reserved_names_are_the_consumers_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".pirate-notes").write_text("mine")
            policy = FilePolicy.parse({"roots": {"work": {"path": directory, "read": True, "write": True}}})
            plain = FileTools(policy, root / "locks-a")
            self.assertEqual(plain._file_call("file_read", {"operation": "file_read", "request_id": "t",
                                                            "root": "work", "path": ".pirate-notes"})["content"], "mine")
            pirate = FileTools(policy, root / "locks-b", reserved_prefixes=(".pirate-",))
            with self.assertRaises(Refusal):
                pirate._file_call("file_read", {"operation": "file_read", "request_id": "t",
                                                "root": "work", "path": ".pirate-notes"})
            for tools in (plain, pirate):
                with self.assertRaises(Refusal):
                    tools._file_call("file_read", {"operation": "file_read", "request_id": "t",
                                                   "root": "work", "path": ".boring-file-x"})


if __name__ == "__main__":
    unittest.main()
