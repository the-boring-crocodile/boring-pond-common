import json
import math

import pytest

from boring_outcomes import CODE, OUR_CODES, Failed, Outcome, Refused, is_code


@pytest.mark.parametrize("cls,status", [(Refused, "refused"), (Failed, "failed")])
@pytest.mark.parametrize("code", ["a", "a" * 64, "vendor_error", "timeout"])
def test_flat_json_outcome_and_same_cause_for_both_statuses(cls, status, code):
    value = cls(code, "explanation", retry_after_seconds=2, nested={"values": [None, True, 1.5]})
    assert isinstance(value, Outcome)
    assert str(value) == f"{code}: explanation"
    assert value.as_data() == {"status": status, "code": code, "message": "explanation",
                               "retry_after_seconds": 2, "nested": {"values": [None, True, 1.5]}}
    json.dumps(value.as_data(), allow_nan=False)


@pytest.mark.parametrize("code", ["", "A", "a-b", "a.b", "a\n", "a" * 65, 1, None, True])
def test_invalid_codes_are_programming_errors(code):
    assert not is_code(code)
    with pytest.raises((TypeError, ValueError)):
        Refused(code, "message")


@pytest.mark.parametrize("message", [None, 1, [], {}])
def test_message_is_a_string(message):
    with pytest.raises(TypeError):
        Failed("bad_reply", message)


@pytest.mark.parametrize("reserved", ["status", "code", "message"])
def test_details_cannot_override_reserved_fields(reserved):
    with pytest.raises((TypeError, ValueError)):
        Refused("busy", "occupied", **{reserved: "override"})


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, b"bytes", object(), {1, 2},
                               {"nested": math.nan}])
def test_invalid_json_details_are_rejected_at_construction(bad):
    with pytest.raises((TypeError, ValueError)):
        Failed("bad_reply", "invalid details", value=bad)


def test_cycles_and_non_string_detail_keys_are_rejected():
    cycle = []
    cycle.append(cycle)
    with pytest.raises(ValueError):
        Refused("busy", "occupied", value=cycle)
    with pytest.raises(TypeError):
        Refused("busy", "occupied", **{1: "bad"})


def test_json_snapshot_does_not_alias_inputs_or_returned_data():
    source = {"items": [1]}
    value = Refused("busy", "occupied", payload=source, coordinates=(1, 2), labels={1: "one"})
    source["items"].append(2)
    first = value.as_data()
    assert first["payload"] == {"items": [1]}
    assert first["coordinates"] == [1, 2]
    assert first["labels"] == {"1": "one"}
    first["payload"]["items"].append(3)
    assert value.as_data()["payload"] == {"items": [1]}
    value.details["status"] = "succeeded"
    with pytest.raises(ValueError):
        value.as_data()


def test_meaning_table_covers_shared_causes_not_a_status_whitelist():
    assert set(OUR_CODES) == {
        "invalid_request", "not_found", "forbidden", "stale", "busy", "locked",
        "limit", "not_admitted", "unavailable", "timeout", "unsupported",
        "unreadable", "conflict", "bad_reply", "cleanup_unconfirmed", "upstream_error"}
    assert all(CODE.fullmatch(code) and meaning.strip() for code, meaning in OUR_CODES.items())
    assert "refused" not in OUR_CODES
    assert is_code("vendor_specific")
    with pytest.raises(TypeError):
        Outcome("busy", "use a concrete status")
