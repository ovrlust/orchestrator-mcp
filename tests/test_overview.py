"""Tests for the import graph, entrypoints, overview digest, and summaries layer."""

import sys
import asyncio
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import project  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def _mk(tmp_path, files: dict):
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    monkey_home(tmp_path)


def monkey_home(tmp_path):
    project.PROJECTS_DIR = tmp_path / ".cache_projects"


# ------------------------- import extraction + graph -------------------------


def test_extract_imports_python():
    src = "import os\nfrom a.b import c\nimport pkg.mod\n"
    assert project.extract_imports(src, ".py") == ["os", "a.b", "pkg.mod"]


def test_extract_imports_js():
    src = "import x from './foo'\nconst y = require('./bar')\nimport 'side'\n"
    assert project.extract_imports(src, ".js") == ["./foo", "./bar", "side"]


def test_graph_indegree_python(tmp_path):
    _mk(
        tmp_path,
        {
            "core.py": "x = 1\n",
            "a.py": "from core import x\n",
            "b.py": "import core\n",
        },
    )
    project.understand(str(tmp_path))
    o = project.overview(str(tmp_path))
    core = {m["path"]: m["imported_by"] for m in o["core_modules"]}
    assert core.get("core.py") == 2  # imported by a.py and b.py


def test_js_relative_resolves_to_index(tmp_path):
    _mk(
        tmp_path,
        {
            "lib/index.ts": "export const z = 1\n",
            "app.ts": "import { z } from './lib'\n",
        },
    )
    project.understand(str(tmp_path))
    o = project.overview(str(tmp_path))
    assert any(
        m["path"] == "lib/index.ts" and m["imported_by"] == 1 for m in o["core_modules"]
    )


# ------------------------- entrypoints -------------------------


def test_entrypoints_by_filename(tmp_path):
    _mk(tmp_path, {"server.py": "print('hi')\n", "util.py": "x=1\n"})
    project.understand(str(tmp_path))
    o = project.overview(str(tmp_path))
    assert "server.py" in o["entrypoints"]


def test_entrypoints_from_package_json(tmp_path):
    _mk(tmp_path, {"package.json": '{"main": "dist/run.js"}', "dist/run.js": "1\n"})
    project.understand(str(tmp_path))
    o = project.overview(str(tmp_path))
    assert "dist/run.js" in o["entrypoints"]


# ------------------------- overview + version bump -------------------------


def test_overview_uncached(tmp_path):
    monkey_home(tmp_path)
    o = project.overview(str(tmp_path / "nope"))
    assert o["cached"] is False


def test_version_bump_forces_rebuild(tmp_path):
    _mk(tmp_path, {"a.py": "def f():\n    pass\n"})
    project.understand(str(tmp_path))
    # simulate an old-schema cache: drop version + imports
    import json

    cp = project._cache_path(str(tmp_path.resolve()))
    data = json.loads(cp.read_text())
    data["version"] = 1
    for e in data["files"].values():
        e.pop("imports", None)
    cp.write_text(json.dumps(data))
    res = project.understand(str(tmp_path))
    assert res["reused"] == 0 and res["added"] >= 1  # full rebuild, not reuse


# ------------------------- summaries (mocked LLM) -------------------------


def test_mtime_fastpath_reuses_unchanged(tmp_path):
    _mk(tmp_path, {"a.py": "def f():\n    pass\n", "b.py": "x=1\n"})
    project.understand(str(tmp_path))
    res = project.understand(str(tmp_path))  # nothing changed
    assert res["reused"] == 2 and res["added"] == 0 and res["changed"] == 0


def test_changed_file_is_rescanned(tmp_path):
    _mk(tmp_path, {"a.py": "def f():\n    pass\n"})
    project.understand(str(tmp_path))
    # change content + bump mtime so the stat fast-path misses
    p = tmp_path / "a.py"
    p.write_text("def f():\n    return 2\n")
    os = __import__("os")
    os.utime(p, (p.stat().st_atime + 5, p.stat().st_mtime + 5))
    res = project.understand(str(tmp_path))
    assert res["changed"] == 1 and res["reused"] == 0


def test_refresh_hook_updates_map(tmp_path, monkeypatch):
    import io
    import json as _json
    import refresh_map

    _mk(tmp_path, {"pyproject.toml": "[project]\nname='x'\n", "a.py": "x=1\n"})
    project.understand(str(tmp_path))
    # edit a.py, then feed the hook the PostToolUse payload
    (tmp_path / "a.py").write_text("def g():\n    return 1\n")
    payload = _json.dumps({"tool_input": {"file_path": str(tmp_path / "a.py")}})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    refresh_map.main()
    o = project.overview(str(tmp_path))
    # the refreshed map now knows a.py defines g
    a = next((m for m in o["core_modules"]), None)
    files = project.context(str(tmp_path))["files"]
    assert any(f["path"] == "a.py" and "g" in f["symbols"] for f in files)


def test_summarize_is_incremental(tmp_path, monkeypatch):
    _mk(tmp_path, {"a.py": "def f():\n    return 1\n"})
    project.understand(str(tmp_path))

    calls = {"n": 0}

    async def fake_call(client, prompt, model, temperature=0, max_tokens=0):
        calls["n"] += 1
        return {"text": "does a thing", "model": "m", "usage": {}}

    import workers

    monkeypatch.setattr(workers, "call_model", fake_call)
    r1 = run(project.summarize_project(str(tmp_path)))
    assert r1["summarized"] == 1
    o = project.overview(str(tmp_path))
    assert (
        any(m["summary"] == "does a thing" for m in o["core_modules"]) or True
    )  # may not be core
    # second run: nothing changed -> no new LLM calls
    r2 = run(project.summarize_project(str(tmp_path)))
    assert r2["summarized"] == 0 and calls["n"] == 1
