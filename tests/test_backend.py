"""Tests for the harness backend: provider conversions, sessions, tools, REST."""

import sys
import asyncio
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from backend import providers, session as sessions, tools  # noqa: E402


def run(c):
    return asyncio.run(c)


# ------------------------- provider format adapters -------------------------


def test_to_openai_tool_calls():
    msgs = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "name": "read_file", "args": {"path": "x"}}],
        }
    ]
    out = providers.to_openai(msgs)
    tc = out[0]["tool_calls"][0]
    assert tc["function"]["name"] == "read_file"
    assert tc["function"]["arguments"] == '{"path": "x"}'


def test_to_anthropic_extracts_system_and_groups_tools():
    msgs = [
        {"role": "system", "content": "be good"},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "ok",
            "tool_calls": [{"id": "c1", "name": "grep", "args": {"pattern": "x"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "match"},
        {"role": "tool", "tool_call_id": "c2", "content": "match2"},
    ]
    system, amsgs = providers.to_anthropic(msgs)
    assert system == "be good"
    # assistant turn carries a tool_use block
    asst = [m for m in amsgs if m["role"] == "assistant"][0]
    assert any(b["type"] == "tool_use" and b["name"] == "grep" for b in asst["content"])
    # the two consecutive tool results collapse into ONE user turn
    user_tool_turns = [
        m for m in amsgs if m["role"] == "user" and isinstance(m["content"], list)
    ]
    assert len(user_tool_turns) == 1
    assert len(user_tool_turns[0]["content"]) == 2


def test_tools_to_anthropic_shape():
    t = providers.tools_to_anthropic(
        [
            {
                "type": "function",
                "function": {
                    "name": "f",
                    "description": "d",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
    )
    assert t[0]["name"] == "f" and "input_schema" in t[0]


# ------------------------- sessions -------------------------


def _home(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "SESS_DIR", tmp_path / "sessions")


def test_session_crud(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    s = sessions.create(str(tmp_path), title="t", provider="openrouter", model="m")
    assert sessions.get(s.id).title == "t"
    assert any(x["id"] == s.id for x in sessions.list_all())
    assert sessions.delete(s.id) is True
    assert sessions.get(s.id) is None


def test_event_log_seq_and_read(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    s = sessions.create(str(tmp_path))
    e1 = sessions.append_event(s.id, {"type": "token", "text": "a"})
    e2 = sessions.append_event(s.id, {"type": "token", "text": "b"})
    assert (e1["seq"], e2["seq"]) == (1, 2)
    assert [e["text"] for e in sessions.read_events(s.id, since=1)] == ["b"]


def test_subscribe_receives_pushed_event(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    s = sessions.create(str(tmp_path))

    async def go():
        q = sessions.subscribe(s.id)
        sessions.append_event(s.id, {"type": "done"})
        e = await asyncio.wait_for(q.get(), timeout=1)
        sessions.unsubscribe(s.id, q)
        return e

    assert run(go())["type"] == "done"


# ------------------------- orchestrator toolset -------------------------


def test_orch_tools_include_delegate_and_spawn_not_done():
    names = {t["function"]["name"] for t in tools.ORCH_TOOLS}
    assert {"delegate", "spawn_agent", "edit_file", "read_file"} <= names
    assert "done" not in names


def test_dispatch_worker_tool(tmp_path):
    (tmp_path / "f.txt").write_text("hello\n")
    ctx = {
        "work": str(tmp_path),
        "allow_cmds": [],
        "seen": set(),
        "changed": set(),
        "model": "",
    }
    out = run(tools.dispatch("read_file", {"path": "f.txt"}, ctx))
    assert "hello" in out


# ------------------------- REST surface -------------------------


def test_rest_crud_and_panels(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    from backend import app as appmod

    client = TestClient(appmod.app)

    assert client.get("/api/health").json()["ok"] is True

    r = client.post("/api/sessions", json={"cwd": str(tmp_path), "title": "demo"})
    assert r.status_code == 200
    sid = r.json()["id"]

    assert any(s["id"] == sid for s in client.get("/api/sessions").json())
    assert client.get(f"/api/sessions/{sid}").json()["title"] == "demo"

    # message bus over REST
    seq = client.post(
        f"/api/sessions/{sid}/messages", json={"text": "hi", "to": "agent"}
    ).json()["seq"]
    assert seq == 1
    bus = client.get(f"/api/sessions/{sid}/messages").json()
    assert bus[0]["text"] == "hi"

    assert client.get(f"/api/sessions/{sid}/board").json() == {}
    assert client.get("/api/models").json()["providers"]

    assert client.delete(f"/api/sessions/{sid}").json()["ok"] is True
    assert client.get(f"/api/sessions/{sid}").status_code == 404
