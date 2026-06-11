"""Tests for Director (parallel agents + deps) and Supervisor (live polling)."""

import sys
import asyncio
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import director  # noqa: E402
import coordination as coord  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def _fake_loop_factory(record=None, errors=None, slow=None):
    errors = errors or set()
    slow = slow or set()

    async def fake_loop(task, work, model, agent_id, allow, max_steps, system):
        if record is not None:
            record.append(agent_id)
        if agent_id in slow:
            await asyncio.sleep(5)  # stays pending so supervisor can cancel it
        if agent_id in errors:
            return {"error": "boom", "files_changed": [], "usage": {}}
        return {"result": "done", "files_changed": [f"{agent_id}.py"], "usage": {}}

    return fake_loop


def _patch(monkeypatch, **kw):
    monkeypatch.setattr(director, "API_KEY", "test", raising=False)
    monkeypatch.setattr(director.workers, "API_KEY", "test")
    monkeypatch.setattr(director, "run_agent_loop", _fake_loop_factory(**kw))


# ------------------------- Director -------------------------


def test_director_runs_all_sections(tmp_path, monkeypatch):
    _patch(monkeypatch)
    secs = [{"id": "a", "task": "do a"}, {"id": "b", "task": "do b"}]
    res = run(director.run_director(secs, str(tmp_path)))
    assert res["summary"]["done"] == 2
    assert {s["id"] for s in res["sections"]} == {"a", "b"}


def test_director_respects_depends_on(tmp_path, monkeypatch):
    order = []
    monkeypatch.setattr(director.workers, "API_KEY", "test")
    monkeypatch.setattr(director, "run_agent_loop", _fake_loop_factory(record=order))
    secs = [
        {"id": "a", "task": "first"},
        {"id": "b", "task": "second", "depends_on": ["a"]},
    ]
    run(director.run_director(secs, str(tmp_path)))
    assert order.index("a") < order.index("b")


def test_director_skips_dependent_when_dep_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(director.workers, "API_KEY", "test")
    monkeypatch.setattr(director, "run_agent_loop", _fake_loop_factory(errors={"a"}))
    secs = [
        {"id": "a", "task": "will fail"},
        {"id": "b", "task": "needs a", "depends_on": ["a"]},
    ]
    res = run(director.run_director(secs, str(tmp_path)))
    by = {s["id"]: s for s in res["sections"]}
    assert by["a"]["status"] == "failed"
    assert by["b"]["status"] == "skipped"


def test_director_dep_result_published_to_board(tmp_path, monkeypatch):
    _patch(monkeypatch)
    secs = [{"id": "a", "task": "x"}]
    run(director.run_director(secs, str(tmp_path)))
    board = coord.board_get(str(tmp_path))
    assert board["a"]["status"] == "done"


def test_director_errors_on_bad_sections(tmp_path, monkeypatch):
    monkeypatch.setattr(director.workers, "API_KEY", "test")
    res = run(director.run_director([{"task": "no id"}], str(tmp_path)))
    assert "error" in res


# ------------------------- Supervisor -------------------------


def test_supervisor_polls_and_messages(tmp_path, monkeypatch):
    _patch(monkeypatch)

    async def fake_decide(client, snap, model):
        return {
            "messages": [{"to": "a", "text": "tighten it up"}],
            "stop": False,
            "note": "ok",
        }

    monkeypatch.setattr(director, "_supervisor_decide", fake_decide)
    secs = [{"id": "a", "task": "do a"}]
    res = run(
        director.run_director(secs, str(tmp_path), supervise=True, poll_interval=0)
    )
    assert res["supervision"]  # at least one poll logged
    msgs = coord_msgs(tmp_path)
    assert any(m["from"] == "supervisor" and m["text"] == "tighten it up" for m in msgs)


def test_supervisor_stop_cancels_running_agents(tmp_path, monkeypatch):
    monkeypatch.setattr(director.workers, "API_KEY", "test")
    monkeypatch.setattr(director, "run_agent_loop", _fake_loop_factory(slow={"a"}))

    async def fake_decide(client, snap, model):
        return {"messages": [], "stop": True, "note": "abort"}

    monkeypatch.setattr(director, "_supervisor_decide", fake_decide)
    secs = [{"id": "a", "task": "slow one"}]
    res = run(
        director.run_director(secs, str(tmp_path), supervise=True, poll_interval=0)
    )
    assert res["sections"][0]["status"] == "failed"  # cancelled -> failed


def coord_msgs(tmp_path):
    import messages as msgbus

    return msgbus.read_messages(str(tmp_path), "", 0)
