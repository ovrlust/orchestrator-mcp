"""Tests for agent-to-agent push delivery, heartbeats, and the monitor view."""

import sys
import asyncio
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import agent  # noqa: E402
import coordination as coord  # noqa: E402
import messages as msgbus  # noqa: E402
import server  # noqa: E402


def run(coro):
    return asyncio.run(coro)


async def _fake_chat(client, body, timeout=None, **k):
    # No tool calls + content => the loop finishes ("done") after one turn.
    return {"choices": [{"message": {"content": "done"}}], "usage": {}}


def test_pending_message_is_pushed_to_agent(tmp_path, monkeypatch):
    work = str(tmp_path)
    msgbus.post_message(work, "orchestrator", "please refactor X", to="a1")
    monkeypatch.setattr(agent, "chat_resilient", _fake_chat)
    run(agent.run_agent_loop("do it", work, agent_id="a1", max_steps=3))
    types = [e["type"] for e in coord.events_tail(work, 50)]
    assert "messages_delivered" in types  # the directive reached the agent


def test_agent_does_not_receive_its_own_posts(tmp_path, monkeypatch):
    work = str(tmp_path)
    msgbus.post_message(work, "a1", "note to self / broadcast", to="")
    monkeypatch.setattr(agent, "chat_resilient", _fake_chat)
    run(agent.run_agent_loop("do it", work, agent_id="a1", max_steps=3))
    types = [e["type"] for e in coord.events_tail(work, 50)]
    assert "messages_delivered" not in types  # no echo of its own message


def test_broadcast_reaches_other_agent(tmp_path, monkeypatch):
    work = str(tmp_path)
    msgbus.post_message(work, "a1", "everyone: schema is frozen", to="")
    monkeypatch.setattr(agent, "chat_resilient", _fake_chat)
    run(agent.run_agent_loop("do it", work, agent_id="a2", max_steps=3))
    delivered = [
        e for e in coord.events_tail(work, 50) if e["type"] == "messages_delivered"
    ]
    assert delivered and delivered[0]["agent"] == "a2"


def test_heartbeat_records_step_and_last_active(tmp_path, monkeypatch):
    work = str(tmp_path)
    monkeypatch.setattr(agent, "chat_resilient", _fake_chat)
    run(agent.run_agent_loop("do it", work, agent_id="a1", max_steps=3))
    reg = coord.reg_get(work)["a1"]
    assert reg.get("step") == 1 and "last_active" in reg


def test_monitor_gives_one_live_view(tmp_path):
    work = str(tmp_path)
    coord.reg_update(work, "a1", status="running", step=2)
    coord.board_set(work, "api_shape", "frozen", agent="a1")
    msgbus.post_message(work, "a1", "hello", to="")
    m = server.monitor(work)
    assert "a1" in m["agents"]
    assert "api_shape" in m["board_keys"]
    assert any(msg["text"] == "hello" for msg in m["messages"])
    assert isinstance(m["events"], list)
