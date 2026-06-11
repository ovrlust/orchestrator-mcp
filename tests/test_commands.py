"""Tests for the slash-command system (network-free except compact, which is
covered at the pure-split level)."""

import sys
import asyncio
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from backend import commands, session as sessions  # noqa: E402


def run(c):
    return asyncio.run(c)


def _home(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "SESS_DIR", tmp_path / "sessions")


# ------------------------- compact split (pure) -------------------------


def test_compact_split_keeps_tail():
    msgs = [{"role": "user", "content": str(i)} for i in range(10)]
    old, recent = commands.compact_split(msgs, 3)
    assert len(old) == 7 and len(recent) == 3


def test_compact_split_does_not_orphan_tool_result():
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "tool_calls": [{"id": "c1", "name": "grep", "args": {}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "r"},
        {"role": "assistant", "content": "done"},
    ]
    # keep_tail=2 would start recent at the 'tool' message; it must back up to the assistant
    old, recent = commands.compact_split(msgs, 2)
    assert recent[0]["role"] == "assistant" and recent[0].get("tool_calls")


def test_compact_split_nothing_to_do():
    msgs = [{"role": "user", "content": "x"}]
    assert commands.compact_split(msgs, 6) == ([], msgs)


# ------------------------- command handlers -------------------------


def test_clear(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    s = sessions.create(str(tmp_path))
    s.messages = [{"role": "user", "content": "a"}]
    r = run(commands.run(s, "/clear"))
    assert r["ok"] and sessions.get(s.id).messages == []


def test_model_set_and_show(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    s = sessions.create(str(tmp_path))
    run(commands.run(s, "/model anthropic claude-sonnet-4-6"))
    got = sessions.get(s.id)
    assert got.provider == "anthropic" and got.model == "claude-sonnet-4-6"


def test_mode_set_and_validate(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    s = sessions.create(str(tmp_path))
    assert run(commands.run(s, "/mode solo"))["ok"]
    assert sessions.get(s.id).mode == "solo"
    assert run(commands.run(s, "/mode bogus"))["ok"] is False


def test_help_lists_commands(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    s = sessions.create(str(tmp_path))
    msg = run(commands.run(s, "/help"))["message"]
    assert "/compact" in msg and "/model" in msg


def test_unknown_command(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    s = sessions.create(str(tmp_path))
    r = run(commands.run(s, "/nope"))
    assert r["ok"] is False and "unknown" in r["message"]


def test_command_emits_event(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    s = sessions.create(str(tmp_path))
    run(commands.run(s, "/mode solo"))
    evs = sessions.read_events(s.id)
    assert any(e["type"] == "command" and e["name"] == "mode" for e in evs)


# ------------------------- toolset reflects mode -------------------------


def test_solo_mode_drops_dispatch_tools():
    from backend import tools

    solo = {t["function"]["name"] for t in tools.toolset("solo")}
    deleg = {t["function"]["name"] for t in tools.toolset("delegate")}
    assert "delegate" not in solo and "spawn_agent" not in solo
    assert "delegate" in deleg and "spawn_agent" in deleg
    assert "read_file" in solo  # direct tools remain


# ------------------------- routed through REST -------------------------


def test_slash_message_routes_to_command(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    from backend import app as appmod

    client = TestClient(appmod.app)
    sid = client.post("/api/sessions", json={"cwd": str(tmp_path)}).json()["id"]
    r = client.post(f"/api/sessions/{sid}/message", json={"text": "/help"})
    assert r.status_code == 200 and "/compact" in r.json()["message"]
    assert any(c["name"] == "compact" for c in client.get("/api/commands").json())
