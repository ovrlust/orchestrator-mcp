"""Tests for the worker tools added for harness parity: multi_edit, glob,
update_plan, web_search (no-key path)."""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import agent  # noqa: E402
import coordination as coord  # noqa: E402


def _ex(name, args, work, seen=None):
    return agent.exec_tool(
        name, args, work, [], set(), "a", seen if seen is not None else set()
    )


def test_multi_edit_atomic(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\ny = 2\n")
    seen = set()
    _ex("read_file", {"path": "a.py"}, str(tmp_path), seen)
    out = _ex(
        "multi_edit",
        {
            "path": "a.py",
            "edits": [
                {"old_string": "x = 1", "new_string": "x = 10"},
                {"old_string": "y = 2", "new_string": "y = 20"},
            ],
        },
        str(tmp_path),
        seen,
    )
    assert "multi-edited" in out
    assert f.read_text() == "x = 10\ny = 20\n"


def test_multi_edit_requires_read_first(tmp_path):
    (tmp_path / "a.py").write_text("a")
    out = _ex(
        "multi_edit",
        {"path": "a.py", "edits": [{"old_string": "a", "new_string": "b"}]},
        str(tmp_path),
    )
    assert "read_file" in out  # gated


def test_multi_edit_atomic_rollback_on_bad_match(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("keep me\n")
    seen = set()
    _ex("read_file", {"path": "a.py"}, str(tmp_path), seen)
    out = _ex(
        "multi_edit",
        {
            "path": "a.py",
            "edits": [
                {"old_string": "keep me", "new_string": "changed"},
                {"old_string": "NOT THERE", "new_string": "x"},
            ],
        },
        str(tmp_path),
        seen,
    )
    assert "ERROR" in out
    assert f.read_text() == "keep me\n"  # nothing written


def test_glob_matches_and_skips_ignored(tmp_path):
    (tmp_path / "a.py").write_text("1")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("2")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.py").write_text("3")
    out = _ex("glob", {"pattern": "**/*.py"}, str(tmp_path))
    files = set(out.splitlines())
    assert files == {"a.py", "sub/b.py"}  # node_modules skipped, symlink-safe relative


def test_update_plan_writes_board(tmp_path):
    out = _ex(
        "update_plan",
        {"plan": [{"text": "x", "done": True}, {"text": "y"}]},
        str(tmp_path),
    )
    assert "1/2 done" in out
    assert coord.board_get(str(tmp_path), "plan") == [
        {"text": "x", "done": True},
        {"text": "y"},
    ]


def test_web_search_without_key(tmp_path, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    out = _ex("web_search", {"query": "hi"}, str(tmp_path))
    assert "unavailable" in out
