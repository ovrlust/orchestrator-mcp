"""Tests for the sub-agent layer: presets (tool filtering), the output-schema
gate, transcript persistence, resume (agent_send), and background spawn/collect."""

import sys
import json
import asyncio
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import agent  # noqa: E402
import presets  # noqa: E402
import subagents  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def _msg(message):
    return {"choices": [{"message": message}], "usage": {}}


def _tc(name, args, cid="c1"):
    return _msg(
        {
            "tool_calls": [
                {
                    "id": cid,
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ]
        }
    )


def scripted(responses, bodies=None):
    """Fake chat_resilient returning canned responses; optionally records bodies."""
    it = iter(responses)

    async def fake(client, body, timeout=None, **k):
        if bodies is not None:
            # snapshot — the loop mutates the live messages list after the call
            bodies.append({**body, "messages": list(body["messages"])})
        return next(it)

    return fake


def _usage(pt, ct):
    return {"prompt_tokens": pt, "completion_tokens": ct}


# ------------------------- redundant-read guard -------------------------


def test_redundant_read_window_is_flagged_not_reserved(tmp_path):
    (tmp_path / "f.txt").write_text("\n".join(f"line {i}" for i in range(20)))
    windows = set()
    a = agent.exec_tool(
        "read_file", {"path": "f.txt", "offset": 1, "limit": 10},
        str(tmp_path), [], set(), "a1", set(), windows,
    )
    b = agent.exec_tool(
        "read_file", {"path": "f.txt", "offset": 1, "limit": 10},
        str(tmp_path), [], set(), "a1", set(), windows,
    )
    assert "line 0" in a  # first read returns content
    assert "already read" in b and "line 0" not in b  # second is flagged, no content


def test_different_window_still_served(tmp_path):
    (tmp_path / "f.txt").write_text("\n".join(f"line {i}" for i in range(40)))
    windows = set()
    agent.exec_tool("read_file", {"path": "f.txt", "offset": 1, "limit": 10},
                    str(tmp_path), [], set(), "a1", set(), windows)
    b = agent.exec_tool("read_file", {"path": "f.txt", "offset": 11, "limit": 10},
                        str(tmp_path), [], set(), "a1", set(), windows)
    assert "line 10" in b  # a different window is real content, not flagged


def test_window_forgotten_after_write(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("\n".join(f"line {i}" for i in range(20)))
    windows, seen = set(), set()
    agent.exec_tool("read_file", {"path": "f.txt", "offset": 1, "limit": 10},
                    str(tmp_path), [], set(), "a1", seen, windows)
    agent.exec_tool("write_file", {"path": "f.txt", "content": "new content\n" * 20},
                    str(tmp_path), [], set(), "a1", seen, windows)
    b = agent.exec_tool("read_file", {"path": "f.txt", "offset": 1, "limit": 10},
                        str(tmp_path), [], set(), "a1", seen, windows)
    assert "new content" in b  # re-read after edit returns fresh content, not flag


# ------------------------- token ceiling -------------------------


def test_token_ceiling_forces_finish(tmp_path, monkeypatch):
    # Each step reports 600 tokens; ceiling 1000 → forced final on the 2nd step.
    def big(content_or_tc):
        return {"choices": [{"message": content_or_tc}], "usage": _usage(500, 100)}

    seq = [
        big({"tool_calls": [{"id": "c1", "function": {"name": "list_dir", "arguments": "{}"}}]}),
        big({"tool_calls": [{"id": "c2", "function": {"name": "list_dir", "arguments": "{}"}}]}),
        big({"content": "forced final answer"}),  # the no-tools extraction call
    ]
    it = iter(seq)

    async def fake(client, body, timeout=None, **k):
        return next(it)

    monkeypatch.setattr(agent, "chat_resilient", fake)
    r = run(
        agent.run_agent_loop(
            "do a long thing", str(tmp_path), max_steps=20, max_total_tokens=1000
        )
    )
    assert r["result"] == "forced final answer"
    assert r["steps"] < 20  # stopped by the token ceiling, not the step cap


# ------------------------- presets -------------------------


def test_explore_toolset_is_read_only(tmp_path, monkeypatch):
    bodies = []
    monkeypatch.setattr(
        agent, "chat_resilient", scripted([_tc("done", {"summary": "ok"})], bodies)
    )
    run(agent.run_agent_loop("look around", str(tmp_path), agent_type="explore"))
    offered = {t["function"]["name"] for t in bodies[0]["tools"]}
    assert offered == presets.tool_names("explore")
    assert "edit_file" not in offered and "run_command" not in offered


def test_explore_blocked_from_editing_even_if_it_tries(tmp_path, monkeypatch):
    (tmp_path / "f.txt").write_text("hello")
    monkeypatch.setattr(
        agent,
        "chat_resilient",
        scripted(
            [
                _tc("edit_file", {"path": "f.txt", "old_string": "hello", "new_string": "x"}),
                _tc("done", {"summary": "report"}),
            ]
        ),
    )
    r = run(agent.run_agent_loop("scout", str(tmp_path), agent_type="explore"))
    assert r["result"] == "report"
    assert (tmp_path / "f.txt").read_text() == "hello"  # nothing written


def test_unknown_agent_type_errors(tmp_path):
    r = run(agent.run_agent_loop("x", str(tmp_path), agent_type="hacker"))
    assert "unknown agent_type" in r["error"]


def test_report_contract_in_system_prompt(tmp_path, monkeypatch):
    bodies = []
    monkeypatch.setattr(
        agent, "chat_resilient", scripted([_tc("done", {"summary": "ok"})], bodies)
    )
    run(agent.run_agent_loop("t", str(tmp_path)))
    assert "REPORT CONTRACT" in bodies[0]["messages"][0]["content"]


def test_map_digest_injected_for_project(tmp_path, monkeypatch):
    # A recognizable project (has a marker) gets the cached overview injected.
    (tmp_path / "requirements.txt").write_text("httpx\n")
    (tmp_path / "main.py").write_text("import util\n")
    (tmp_path / "util.py").write_text("def f():\n    return 1\n")
    bodies = []
    monkeypatch.setattr(
        agent, "chat_resilient", scripted([_tc("done", {"summary": "ok"})], bodies)
    )
    run(agent.run_agent_loop("t", str(tmp_path), agent_type="explore"))
    sysmsg = bodies[0]["messages"][0]["content"]
    assert "Codebase overview" in sysmsg  # auto-built and injected


def test_map_digest_absent_for_non_project(tmp_path, monkeypatch):
    bodies = []
    monkeypatch.setattr(
        agent, "chat_resilient", scripted([_tc("done", {"summary": "ok"})], bodies)
    )
    run(agent.run_agent_loop("t", str(tmp_path), agent_type="explore"))
    assert "Codebase overview" not in bodies[0]["messages"][0]["content"]


# ------------------------- output schema -------------------------

SCHEMA = {"type": "object", "required": ["answer"], "properties": {"answer": {}}}


def test_schema_rejects_then_accepts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        agent,
        "chat_resilient",
        scripted(
            [
                _tc("done", {"summary": "not json at all"}),
                _tc("done", {"summary": '{"answer": 42}'}),
            ]
        ),
    )
    r = run(
        agent.run_agent_loop("t", str(tmp_path), max_steps=3, output_schema=SCHEMA)
    )
    assert r["result"] == {"answer": 42}  # parsed object, not text


def test_schema_strips_code_fences(tmp_path, monkeypatch):
    monkeypatch.setattr(
        agent,
        "chat_resilient",
        scripted([_tc("done", {"summary": '```json\n{"answer": 1}\n```'})]),
    )
    r = run(
        agent.run_agent_loop("t", str(tmp_path), max_steps=2, output_schema=SCHEMA)
    )
    assert r["result"] == {"answer": 1}


def test_schema_exhausts_retries(tmp_path, monkeypatch):
    bad = _tc("done", {"summary": "still not json"})
    monkeypatch.setattr(agent, "chat_resilient", scripted([bad, bad, bad, bad]))
    r = run(
        agent.run_agent_loop("t", str(tmp_path), max_steps=2, output_schema=SCHEMA)
    )
    assert "failed schema" in r["error"]


def test_schema_applies_to_plain_text_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(
        agent,
        "chat_resilient",
        scripted([_msg({"content": "chatty answer"}), _msg({"content": '{"answer": "x"}'})]),
    )
    r = run(
        agent.run_agent_loop("t", str(tmp_path), max_steps=3, output_schema=SCHEMA)
    )
    assert r["result"] == {"answer": "x"}


# ------------------------- persistence + resume -------------------------


def test_run_and_persist_saves_transcript(tmp_path, monkeypatch):
    work = str(tmp_path)
    monkeypatch.setattr(
        agent, "chat_resilient", scripted([_tc("done", {"summary": "first answer"})])
    )
    r = run(
        subagents.run_and_persist(work, "task one", "", "a1", [], 5, "", "general")
    )
    assert r["result"] == "first answer"
    assert "messages" not in r  # transcript stays on disk, not in the report
    rec = subagents.load(work, "a1")
    assert rec["status"] == "done"
    assert rec["messages"][0]["role"] == "system"
    assert any("task one" in str(m.get("content")) for m in rec["messages"])


def test_send_resumes_with_context(tmp_path, monkeypatch):
    work = str(tmp_path)
    monkeypatch.setattr(
        agent, "chat_resilient", scripted([_tc("done", {"summary": "v1"})])
    )
    run(subagents.run_and_persist(work, "build the thing", "", "a1", [], 5, "", "general"))
    before = len(subagents.load(work, "a1")["messages"])

    bodies = []
    monkeypatch.setattr(
        agent, "chat_resilient", scripted([_tc("done", {"summary": "v2"})], bodies)
    )
    r = run(subagents.send(work, "a1", "now also handle the edge case"))
    assert r["result"] == "v2"
    # The resumed call saw the WHOLE old transcript plus the follow-up.
    sent = bodies[0]["messages"]
    assert len(sent) > before
    assert sent[-1]["content"] == "now also handle the edge case"
    assert any("build the thing" in str(m.get("content")) for m in sent)
    assert len(subagents.load(work, "a1")["messages"]) > before


def test_send_unknown_agent_errors(tmp_path):
    r = run(subagents.send(str(tmp_path), "ghost", "hi"))
    assert "no agent" in r["error"]


def test_valid_id_blocks_path_escape():
    assert not subagents.valid_id("../evil")
    assert not subagents.valid_id("")
    assert not subagents.valid_id(".hidden")
    assert subagents.valid_id("explore-a1_2.x")


# ------------------------- background spawn -------------------------


def test_spawn_and_collect(tmp_path, monkeypatch):
    work = str(tmp_path)
    monkeypatch.setattr(
        agent, "chat_resilient", scripted([_tc("done", {"summary": "bg done"})])
    )

    async def flow():
        sp = subagents.spawn(work, "background task", "", "bg1", [], 5, "", "general")
        assert sp["status"] == "running"
        r = await subagents.result(work, "bg1", wait_seconds=5)
        assert r["status"] == "done" and r["result"] == "bg done"
        # Collected again later: served from the persisted record.
        r2 = await subagents.result(work, "bg1")
        assert r2["status"] == "done" and r2["result"] == "bg done"

    run(flow())


def test_spawn_rejects_duplicate_live_id(tmp_path, monkeypatch):
    work = str(tmp_path)

    async def slow_chat(client, body, timeout=None, **k):
        await asyncio.sleep(30)

    monkeypatch.setattr(agent, "chat_resilient", slow_chat)

    async def flow():
        subagents.spawn(work, "t", "", "dup", [], 5, "", "general")
        sp2 = subagents.spawn(work, "t", "", "dup", [], 5, "", "general")
        assert "already running" in sp2["error"]
        # Mid-run send routes to the live message bus, not a resume.
        s = await subagents.send(work, "dup", "steer left")
        assert s["status"] == "delivered_live"
        subagents._TASKS[(work, "dup")].cancel()

    run(flow())


def test_result_unknown_agent_errors(tmp_path):
    r = run(subagents.result(str(tmp_path), "nobody"))
    assert "no agent" in r["error"]


def test_orphan_without_checkpoint_not_resumable(tmp_path):
    work = str(tmp_path)
    subagents.save(work, "zombie", {"agent_id": "zombie", "status": "running"})
    r = run(subagents.result(work, "zombie"))
    assert r["status"] == "orphaned" and r["resumable"] is False


def test_orphan_with_checkpoint_is_resumable(tmp_path):
    work = str(tmp_path)
    subagents.save(
        work,
        "zombie",
        {
            "agent_id": "zombie",
            "status": "running",
            "step": 3,
            "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}],
        },
    )
    r = run(subagents.result(work, "zombie"))
    assert r["status"] == "orphaned" and r["resumable"] is True


def test_checkpoint_persists_each_step(tmp_path, monkeypatch):
    work = str(tmp_path)
    # Two tool turns then done — checkpoints should land before the run ends.
    monkeypatch.setattr(
        agent,
        "chat_resilient",
        scripted(
            [
                _tc("list_dir", {}),
                _tc("list_dir", {}),
                _tc("done", {"summary": "ok"}),
            ]
        ),
    )
    run(subagents.run_and_persist(work, "t", "", "cp1", [], 10, "", "general"))
    rec = subagents.load(work, "cp1")
    assert rec["status"] == "done"  # final state persisted
    assert rec["messages"]  # transcript on disk


def test_stop_cancels_and_leaves_resumable_checkpoint(tmp_path, monkeypatch):
    work = str(tmp_path)
    started = asyncio.Event()

    async def slow_chat(client, body, timeout=None, **k):
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(agent, "chat_resilient", slow_chat)

    async def flow():
        subagents.spawn(work, "long task", "", "stopme", [], 10, "", "general")
        await started.wait()  # ensure it past the first checkpoint
        await asyncio.sleep(0.05)
        s = subagents.stop(work, "stopme")
        assert s["status"] == "stopping"
        r = await subagents.result(work, "stopme", wait_seconds=5)
        assert r["status"] == "stopped"
        rec = subagents.load(work, "stopme")
        assert rec["status"] == "stopped" and rec["messages"]  # resumable

    run(flow())


def test_stop_unknown_agent_errors(tmp_path):
    r = subagents.stop(str(tmp_path), "ghost")
    assert "no running agent" in r["error"]
