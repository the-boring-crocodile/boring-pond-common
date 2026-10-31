"""Non-reading admission uses FileTools authority; data checks stay at execution."""
from unittest.mock import patch

import pytest

from boring_worker_kit import file_refs
from boring_worker_kit.file_policy import FilePolicy
from boring_worker_kit.files import FileTools
from boring_worker_kit.outcomes import Refusal


@pytest.fixture
def files(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    policy = FilePolicy.parse({"roots": {
        "work": {"path": str(work), "read": True, "write": True, "deny": ["private"]},
        "notes": {"path": str(tmp_path / "notes"), "read": True, "write": False}}})
    tools = FileTools(policy, tmp_path / "locks")
    try:
        yield tools
    finally:
        tools.pool.shutdown(wait=True)


def ref(path="later.txt", root="work", **extra):
    return {"$file": {"root": root, "path": path, **extra}}


@pytest.mark.parametrize("arguments,save_to,code", [
    ({"x": {"$file": "wrong"}}, None, "invalid_request"),
    ({"x": ref(root="foreign")}, None, "forbidden"),
    ({"x": ref("private/x.txt")}, None, "forbidden"),
    ({"x": [ref()] * 17}, None, "limit"),
    (ref(), None, "invalid_request"),
    ([], None, "invalid_request"),
    ({}, {"root": "notes", "path": "new.txt", "expected_sha256": None}, "forbidden"),
    ({}, {"root": "work", "path": "new.txt", "expected_sha256": "bad"}, "invalid_request"),
])
def test_invalid_calls_refused_without_reading(files, arguments, save_to, code):
    with patch.object(files, "call", side_effect=AssertionError("admission must not read")):
        with pytest.raises(Refusal) as caught:
            file_refs.prepare_call(files, arguments, save_to)
    assert caught.value.code == code


def test_valid_admission_does_not_check_existence_or_current_hash(files):
    arguments = {"nested": [ref("not/created/yet.txt", sha256="0" * 64)] * 16}
    target = {"root": "work", "path": "later/result.txt", "expected_sha256": "1" * 64}
    with patch.object(files, "call", side_effect=AssertionError("admission must not read")):
        file_refs.prepare_call(files, arguments, target)
    assert arguments["nested"][0]["$file"]["path"] == "not/created/yet.txt"
    assert target["expected_sha256"] == "1" * 64
