"""Tests for the durable per-tool-call log."""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import toollog  # noqa: E402
import coordination as coord  # noqa: E402


def test_log_and_tail(tmp_path):
    w = str(tmp_path)
    toollog.log_call(w, "a1", 1, "read_file", {"path": "x.py"}, "contents", ok=True)
    toollog.log_call(w, "a1", 2, "grep", {"pattern": "foo"}, "(no matches)", ok=True)
    rows = toollog.tail(w)
    assert [r["fn"] for r in rows] == ["read_file", "grep"]
    assert rows[0]["step"] == 1 and rows[0]["agent"] == "a1"


def test_filter_by_fn_and_agent(tmp_path):
    w = str(tmp_path)
    toollog.log_call(w, "a1", 1, "read_file", {}, "ok")
    toollog.log_call(w, "a2", 1, "edit_file", {}, "ok")
    assert [r["fn"] for r in toollog.tail(w, fn="edit_file")] == ["edit_file"]
    assert [r["agent"] for r in toollog.tail(w, agent="a1")] == ["a1"]


def test_errors_only_and_ok_flag(tmp_path):
    w = str(tmp_path)
    toollog.log_call(w, "a", 1, "edit_file", {}, "done", ok=True)
    toollog.log_call(w, "a", 2, "edit_file", {}, "ERROR: nope", ok=False)
    errs = toollog.tail(w, errors_only=True)
    assert len(errs) == 1 and errs[0]["result"].startswith("ERROR")


def test_truncation_bounds_lines(tmp_path):
    w = str(tmp_path)
    toollog.log_call(w, "a", 1, "run_command", {"cmd": "x" * 9999}, "y" * 9999)
    r = toollog.tail(w)[0]
    assert len(r["args"]) <= toollog.ARGS_MAX + 20  # + "…(+N)" suffix
    assert len(r["result"]) <= toollog.RESULT_MAX + 20


def test_limit_returns_most_recent(tmp_path):
    w = str(tmp_path)
    for i in range(10):
        toollog.log_call(w, "a", i, "grep", {}, "r")
    rows = toollog.tail(w, limit=3)
    assert [r["step"] for r in rows] == [7, 8, 9]


def test_tail_missing_file_is_empty(tmp_path):
    assert toollog.tail(str(tmp_path)) == []


def test_coord_reset_wipes_toollog(tmp_path):
    w = str(tmp_path)
    toollog.log_call(w, "a", 1, "grep", {}, "r")
    assert toollog.tail(w)
    coord.coord_clear(w)
    assert toollog.tail(w) == []
