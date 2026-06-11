"""Tests for the per-project understanding cache: scan, symbols, incremental reuse."""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import project  # noqa: E402


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(project, "PROJECTS_DIR", tmp_path / "_cache")
    proj = tmp_path / "proj"
    (proj / "sub").mkdir(parents=True)
    (proj / "a.py").write_text("def foo():\n    pass\nclass Bar:\n    pass\n")
    (proj / "sub" / "b.ts").write_text("export function baz() {}\n")
    (proj / "node_modules").mkdir()
    (proj / "node_modules" / "junk.js").write_text("function ignored(){}")
    return str(proj)


def test_extract_symbols():
    assert "foo" in project.extract_symbols("def foo():\n class Bar:", ".py")
    assert "Bar" in project.extract_symbols("def foo():\nclass Bar:", ".py")
    assert "baz" in project.extract_symbols("export function baz() {}", ".ts")


def test_understand_and_context(tmp_path, monkeypatch):
    proj = _setup(tmp_path, monkeypatch)
    stats = project.understand(proj)
    assert stats["total_files"] == 2  # node_modules ignored
    assert stats["added"] == 2 and stats["reused"] == 0

    ctx = project.context(proj)
    assert ctx["cached"] is True and ctx["total_files"] == 2
    bypath = {f["path"]: f for f in ctx["files"]}
    assert set(bypath["a.py"]["symbols"]) == {"foo", "Bar"}
    assert bypath["sub/b.ts"]["symbols"] == ["baz"]


def test_incremental_reuse_change_remove(tmp_path, monkeypatch):
    proj = _setup(tmp_path, monkeypatch)
    project.understand(proj)

    # nothing changed -> all reused, no re-read
    s2 = project.understand(proj)
    assert s2["reused"] == 2 and s2["added"] == 0 and s2["changed"] == 0

    # change one file -> exactly one changed, the rest reused
    (pathlib.Path(proj) / "a.py").write_text("def foo():\n    return 1\n")
    s3 = project.understand(proj)
    assert s3["changed"] == 1 and s3["reused"] == 1

    # remove a file -> reported removed
    (pathlib.Path(proj) / "sub" / "b.ts").unlink()
    s4 = project.understand(proj)
    assert s4["removed"] == 1


def test_context_without_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(project, "PROJECTS_DIR", tmp_path / "_cache")
    (tmp_path / "empty").mkdir()
    ctx = project.context(str(tmp_path / "empty"))
    assert ctx["cached"] is False and "hint" in ctx
