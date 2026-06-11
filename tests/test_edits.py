"""Unit tests for the surgical-edit core."""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import edits  # noqa: E402


# ------------------------- apply_edits -------------------------


def test_single_edit():
    assert edits.apply_one("hello world", "world", "there") == "hello there"


def test_delete_via_empty_new():
    assert edits.apply_one("a b c", " b", "") == "a c"


def test_sequential_edits_see_prior_result():
    out = edits.apply_edits("x=1", [{"old": "x", "new": "y"}, {"old": "1", "new": "2"}])
    assert out == "y=2"


def test_not_found_raises():
    try:
        edits.apply_one("abc", "zzz", "q")
        assert False
    except edits.EditError as e:
        assert "not found" in str(e)


def test_ambiguous_raises_without_replace_all():
    try:
        edits.apply_one("a a a", "a", "b")
        assert False
    except edits.EditError as e:
        assert "appears 3 times" in str(e)


def test_replace_all():
    assert edits.apply_one("a a a", "a", "b", replace_all=True) == "b b b"


def test_noop_raises():
    try:
        edits.apply_one("abc", "a", "a")
        assert False
    except edits.EditError as e:
        assert "no-op" in str(e)


def test_old_string_alias():
    assert edits.apply_edits("hi", [{"old_string": "hi", "new_string": "yo"}]) == "yo"


def test_non_string_raises():
    try:
        edits.apply_edits("x", [{"old": 1, "new": "y"}])
        assert False
    except edits.EditError:
        pass


def test_partial_failure_does_not_mutate_caller():
    # second edit fails; function raises and the caller's original is untouched
    src = "keep this"
    try:
        edits.apply_edits(
            src, [{"old": "keep", "new": "drop"}, {"old": "MISSING", "new": "x"}]
        )
        assert False
    except edits.EditError:
        pass
    assert src == "keep this"


# ------------------------- parse_edit_payload -------------------------


def test_parse_plain_array():
    assert edits.parse_edit_payload('[{"old":"a","new":"b"}]') == [
        {"old": "a", "new": "b"}
    ]


def test_parse_single_object_wrapped():
    assert edits.parse_edit_payload('{"old":"a","new":"b"}') == [
        {"old": "a", "new": "b"}
    ]


def test_parse_fenced_json():
    payload = '```json\n[{"old":"a","new":"b"}]\n```'
    assert edits.parse_edit_payload(payload) == [{"old": "a", "new": "b"}]


def test_parse_invalid_raises():
    try:
        edits.parse_edit_payload("not json")
        assert False
    except edits.EditError:
        pass
