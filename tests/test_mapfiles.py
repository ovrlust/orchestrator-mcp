"""Tests for map_files: glob matching, dry-run, and a real bulk transform
(faked worker) with per-file apply + validate + rollback."""

import sys
import asyncio
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import workers  # noqa: E402
import delegate  # noqa: E402
import mapfiles  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def _seed(tmp_path):
    (tmp_path / "a.py").write_text("print('a')\n")
    (tmp_path / "b.py").write_text("print('b')\n")
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "c.py").write_text("print('c')\n")
    (tmp_path / "notes.txt").write_text("ignore me\n")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "junk.py").write_text("x\n")


# ------------------------- matching -------------------------


def test_match_files_globs_and_ignores(tmp_path):
    _seed(tmp_path)
    rels = mapfiles.match_files(str(tmp_path), "**/*.py")
    assert rels == ["a.py", "b.py", "pkg/c.py"]  # sorted, no __pycache__


def test_match_files_exclude(tmp_path):
    _seed(tmp_path)
    rels = mapfiles.match_files(str(tmp_path), "*.py", exclude="b.py")
    assert rels == ["a.py"]


def test_dry_run_lists_without_running(tmp_path):
    _seed(tmp_path)
    r = run(mapfiles.run_map_files(str(tmp_path), "**/*.py", "do x", dry_run=True))
    assert r["dry_run"] and r["matched"] == 3 and r["files"] == ["a.py", "b.py", "pkg/c.py"]
    # unchanged on disk
    assert (tmp_path / "a.py").read_text() == "print('a')\n"


def test_no_match_is_clean_error(tmp_path):
    _seed(tmp_path)
    r = run(mapfiles.run_map_files(str(tmp_path), "**/*.rs", "do x"))
    assert r["matched"] == 0 and "no files matched" in r["error"]


def test_max_files_truncates(tmp_path):
    _seed(tmp_path)
    r = run(mapfiles.run_map_files(str(tmp_path), "**/*.py", "x", max_files=2, dry_run=True))
    assert r["matched"] == 2 and r["truncated"] is True


# ------------------------- real transform (faked worker) -------------------------


def _fake_uppercase(monkeypatch):
    """Worker that upper-cases whatever file content it's handed."""

    async def fake_call_model(client, prompt, model, system="", temperature=0.0,
                              max_tokens=0, fallback=""):
        body = prompt.split("--- CURRENT CONTENT OF", 1)[1]
        content = body.split("---\n", 1)[1]
        return {"text": content.upper(), "model": model or "fake", "usage": {}}

    monkeypatch.setattr(workers, "API_KEY", "test")
    monkeypatch.setattr(delegate, "call_model", fake_call_model)


def test_bulk_transform_applies_to_every_file(tmp_path, monkeypatch):
    _seed(tmp_path)
    _fake_uppercase(monkeypatch)
    r = run(mapfiles.run_map_files(str(tmp_path), "**/*.py", "uppercase it"))
    assert r["summary"]["applied"] == 3
    assert r["summary"]["matched"] == 3
    assert (tmp_path / "a.py").read_text() == "PRINT('A')\n"
    assert (tmp_path / "pkg/c.py").read_text() == "PRINT('C')\n"
    assert (tmp_path / "b.py").read_text() == "PRINT('B')\n"


def test_code_fence_stripped_from_full_file_output(tmp_path, monkeypatch):
    import workers as w

    (tmp_path / "a.py").write_text("x = 1\n")

    async def fenced(client, prompt, model, system="", temperature=0.0,
                     max_tokens=0, fallback=""):
        return {"text": "```python\nx = 2\n```", "model": "fake", "usage": {}}

    monkeypatch.setattr(w, "API_KEY", "test")
    monkeypatch.setattr(delegate, "call_model", fenced)
    run(mapfiles.run_map_files(str(tmp_path), "a.py", "set x to 2"))
    # the wrapping ```python fence is gone, only the real content written
    assert (tmp_path / "a.py").read_text() == "x = 2"


def test_strip_code_fence_leaves_unfenced_alone():
    assert delegate._strip_code_fence("x = 1\ny = 2") == "x = 1\ny = 2"
    assert delegate._strip_code_fence("```py\nx = 1\n```") == "x = 1"
    # a fence that's only part of the content (e.g. a doc example) is NOT stripped
    body = "text\n```\ncode\n```\nmore"
    assert delegate._strip_code_fence(body) == body


def test_validator_failure_rolls_back(tmp_path, monkeypatch):
    _seed(tmp_path)
    _fake_uppercase(monkeypatch)
    # Require lowercase 'print' — every uppercased result fails, so all roll back.
    r = run(
        mapfiles.run_map_files(
            str(tmp_path), "*.py", "uppercase it",
            validate={"type": "regex", "pattern": "print"},
        )
    )
    assert r["summary"]["failed"] == 2
    assert (tmp_path / "a.py").read_text() == "print('a')\n"  # original restored
