"""Tests for token-cheap retrieval: ranged read_file + names-first grep."""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import agent  # noqa: E402


def _ex(name, args, work, seen=None):
    return agent.exec_tool(
        name, args, work, [], set(), "a", seen if seen is not None else set()
    )


# ------------------------- ranged read_file -------------------------


def test_read_file_numbered_and_windowed(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, 101)))
    out = _ex("read_file", {"path": "f.txt", "offset": 10, "limit": 5}, str(tmp_path))
    assert "10\tline10" in out
    assert "14\tline14" in out
    assert "line15" not in out  # window respected
    assert "more lines" in out  # pagination hint
    assert "lines 10-14 of 100" in out


def test_read_file_default_window_truncates(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("\n".join(["x"] * 1000))
    out = _ex("read_file", {"path": "big.txt"}, str(tmp_path))
    # default window is DEFAULT_READ_LINES, not the whole 1000 lines
    assert f"read offset={agent.DEFAULT_READ_LINES + 1}" in out


def test_read_file_marks_seen_for_edit(tmp_path):
    (tmp_path / "f.py").write_text("a = 1\n")
    seen = set()
    _ex("read_file", {"path": "f.py"}, str(tmp_path), seen)
    # edit now permitted because the file was read
    res = agent.exec_tool(
        "edit_file",
        {"path": "f.py", "old_string": "a = 1", "new_string": "a = 2"},
        str(tmp_path),
        [],
        set(),
        "a",
        seen,
    )
    assert "edited" in res


# ------------------------- grep (names-first) -------------------------


def test_grep_names_first_default(tmp_path):
    (tmp_path / "a.py").write_text("import os\nimport os\n")
    (tmp_path / "b.py").write_text("print('hi')\n")
    out = _ex("grep", {"pattern": "import os"}, str(tmp_path))
    assert "a.py" in out
    assert "b.py" not in out
    # default mode is files:count and nudges toward content=true
    assert "content=true" in out


def test_grep_content_mode_shows_lines(tmp_path):
    (tmp_path / "a.py").write_text("alpha\nbeta\nalpha\n")
    out = _ex("grep", {"pattern": "alpha", "content": True}, str(tmp_path))
    assert "a.py:1:alpha" in out
    assert "a.py:3:alpha" in out


def test_grep_no_match(tmp_path):
    (tmp_path / "a.py").write_text("nothing here\n")
    assert _ex("grep", {"pattern": "zzzz"}, str(tmp_path)) == "(no matches)"


def test_grep_fallback_skips_ignore_dirs(tmp_path):
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "junk.py").write_text("target\n")
    (tmp_path / "real.py").write_text("target\n")
    # exercise the pure-Python fallback directly (independent of rg presence)
    out = agent._grep_fallback(str(tmp_path), "target", ".", False, 50)
    assert "real.py" in out
    assert ".venv" not in out
