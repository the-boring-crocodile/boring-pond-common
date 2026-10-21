"""Exact edit policy shared by the Linux and Windows adapters."""
import pytest

from boring_file_core.textpatch import PatchError, apply_edits, checked_edits


@pytest.mark.parametrize("text,old,count", [
    ("aaa", "aa", 2), ("aaaaa", "aaa", 3), ("same same", "same", 2),
    ("absent", "missing", 0),
])
def test_ambiguous_or_missing_matches(text, old, count):
    with pytest.raises(PatchError) as caught:
        apply_edits(text, [(old, "replacement")])
    assert caught.value.details == {"edit": 0, "matches": count}


def test_edits_use_original_normalized_text_in_any_order():
    edits = [("second", "first"), ("first\r\n", "one\r\n")]
    assert apply_edits("first\nsecond", edits) == "one\nfirst"
    assert apply_edits("first\nsecond", list(reversed(edits))) == "one\nfirst"


def test_distinct_but_overlapping_edits_are_refused():
    with pytest.raises(PatchError, match="overlapping edits"):
        apply_edits("abcdef", [("abc", "x"), ("cde", "y")])


@pytest.mark.parametrize("edits", [[], [("", "x")], [("text", "ok"), ("", "bad")]])
def test_empty_edits_are_refused(edits):
    with pytest.raises(PatchError):
        apply_edits("text", edits)
    with pytest.raises(PatchError):
        checked_edits(edits)


def test_preflight_normalizes_without_reading_a_document():
    edits = [("old\r\nline", "new\rline")]
    assert checked_edits(edits) == [("old\nline", "new\nline")]
    assert edits == [("old\r\nline", "new\rline")]
