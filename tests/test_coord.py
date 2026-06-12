"""Unit tests for the coordination layer: board, registry, events, hooks, DAG.

All network-free. The DAG scheduler is `plan_ready`, a pure function so it can be
tested without calling any worker.
"""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import ledger  # noqa: E402
import coordination as coord  # noqa: E402


# ------------------------- blackboard -------------------------


def test_board_set_get(tmp_path):
    w = str(tmp_path)
    assert coord.board_get(w) == {}
    coord.board_set(w, "k", {"v": 1}, agent="a1")
    assert coord.board_get(w, "k") == {"v": 1}
    assert coord.board_get(w) == {"k": {"v": 1}}


def test_board_set_logs_event(tmp_path):
    w = str(tmp_path)
    coord.board_set(w, "k", "v")
    evs = coord.events_tail(w)
    assert any(e["type"] == "board_set" and e["key"] == "k" for e in evs)


def test_board_append_accumulates(tmp_path):
    w = str(tmp_path)
    assert coord.board_append(w, "messages", {"from": "human", "text": "hi"}) == 1
    assert coord.board_append(w, "messages", {"from": "a1", "text": "yo"}) == 2
    msgs = coord.board_get(w, "messages")
    assert [m["from"] for m in msgs] == ["human", "a1"]


def test_board_append_resets_non_list(tmp_path):
    w = str(tmp_path)
    coord.board_set(w, "messages", "not a list")
    assert coord.board_append(w, "messages", {"text": "x"}) == 1


# ------------------------- registry -------------------------


def test_registry_update_merges(tmp_path):
    w = str(tmp_path)
    coord.reg_update(w, "a1", task="do x", status="running")
    coord.reg_update(w, "a1", status="applied", attempts=2)
    r = coord.reg_get(w)
    assert r["a1"]["task"] == "do x"
    assert r["a1"]["status"] == "applied"
    assert r["a1"]["attempts"] == 2


# ------------------------- events -------------------------


def test_events_append_and_tail(tmp_path):
    w = str(tmp_path)
    for i in range(5):
        coord.event(w, "start", f"a{i}")
    evs = coord.events_tail(w, limit=3)
    assert len(evs) == 3
    assert evs[-1]["agent"] == "a4"
    assert "ts" in evs[0]


def test_coord_clear_keeps_ledger(tmp_path):
    w = str(tmp_path)
    coord.board_set(w, "k", "v")
    ledger.record_spend(w, "openai/gpt-4o-mini", {"prompt_tokens": 1000})
    coord.coord_clear(w)
    assert coord.board_get(w) == {}
    assert coord.events_tail(w) == []
    assert ledger.spend_summary(w)["calls"] == 1  # ledger survives


# ------------------------- hooks -------------------------


def test_hook_runs_when_allowed(tmp_path):
    w = str(tmp_path)
    out = coord.run_hook(
        w,
        {"on_finish": "echo {id} {status}"},
        "on_finish",
        {"id": "a1", "status": "applied"},
        ["echo"],
    )
    assert "exit=0" in out


def test_hook_denied_when_not_allowed(tmp_path):
    w = str(tmp_path)
    out = coord.run_hook(
        w, {"on_finish": "echo hi"}, "on_finish", {"id": "a1"}, allow_cmds=[]
    )
    assert "denied" in out
    assert "allow_commands" in out


def test_hook_denied_dangerous(tmp_path):
    w = str(tmp_path)
    out = coord.run_hook(
        w,
        {"on_fail": "rm -rf {output_path}"},
        "on_fail",
        {"id": "a1", "output_path": "/tmp/x"},
        ["rm"],
    )
    assert "denied" in out.lower()


def test_hook_absent_returns_none(tmp_path):
    assert coord.run_hook(str(tmp_path), {}, "on_start", {"id": "a"}, []) is None
    assert coord.run_hook(str(tmp_path), None, "on_start", {"id": "a"}, []) is None


# ------------------------- DAG scheduler (plan_ready) -------------------------


def test_plan_ready_no_deps_all_ready():
    by_id = {"a": {"id": "a"}, "b": {"id": "b"}}
    ready, skip = coord.plan_ready({"a", "b"}, by_id, {})
    assert set(ready) == {"a", "b"} and skip == []


def test_plan_ready_chain_gates():
    by_id = {
        "a": {"id": "a"},
        "b": {"id": "b", "depends_on": ["a"]},
        "c": {"id": "c", "depends_on": ["b"]},
    }
    ready, _ = coord.plan_ready({"a", "b", "c"}, by_id, {})
    assert ready == ["a"]
    ready, _ = coord.plan_ready({"b", "c"}, by_id, {"a": {"status": "applied"}})
    assert ready == ["b"]


def test_plan_ready_skips_on_failed_dep():
    by_id = {"a": {"id": "a"}, "b": {"id": "b", "depends_on": ["a"]}}
    ready, skip = coord.plan_ready({"b"}, by_id, {"a": {"status": "failed"}})
    assert ready == []
    assert skip == [("b", ["a"])]


def test_plan_ready_unknown_dep_treated_satisfied():
    by_id = {"b": {"id": "b", "depends_on": ["ghost"]}}
    ready, skip = coord.plan_ready({"b"}, by_id, {})
    assert ready == ["b"] and skip == []


# ------------------------- work claims (anti-overlap) -------------------------


def test_claim_grants_then_blocks_sibling(tmp_path):
    w = str(tmp_path)
    a = coord.claim_work(w, "agentA", ["f1", "f2", "f3"])
    assert a["granted"] == ["f1", "f2", "f3"] and a["taken"] == {}
    # sibling tries overlapping set — only the free one is granted
    b = coord.claim_work(w, "agentB", ["f3", "f4"])
    assert b["granted"] == ["f4"]
    assert b["taken"] == {"f3": "agentA"}


def test_claim_is_idempotent_for_same_agent(tmp_path):
    w = str(tmp_path)
    coord.claim_work(w, "a", ["x"])
    again = coord.claim_work(w, "a", ["x", "y"])
    assert again["granted"] == ["x", "y"] and again["taken"] == {}


def test_release_frees_for_reassignment(tmp_path):
    w = str(tmp_path)
    coord.claim_work(w, "a", ["x", "y"])
    n = coord.release_work(w, "a", ["x"])
    assert n == 1
    b = coord.claim_work(w, "b", ["x", "y"])
    assert b["granted"] == ["x"] and b["taken"] == {"y": "a"}


# ------------------------- aggregate (N reports -> 1) -------------------------


def test_aggregate_merges_and_dedups(tmp_path):
    w = str(tmp_path)
    coord.board_set(w, "scoutA", ["bug in auth.py", "slow query"], agent="a")
    coord.board_set(w, "scoutB", ["slow query", "missing index"], agent="b")
    agg = coord.aggregate_board(w)
    assert agg["n_sources"] == 2
    assert agg["items"].count("slow query") == 1  # deduped across agents
    assert set(agg["items"]) == {"bug in auth.py", "slow query", "missing index"}


def test_aggregate_keys_filter_and_no_dedup(tmp_path):
    w = str(tmp_path)
    coord.board_set(w, "a", ["x", "x"], agent="a")
    coord.board_set(w, "b", ["y"], agent="b")
    only_a = coord.aggregate_board(w, keys=["a"], dedup=False)
    assert only_a["items"] == ["x", "x"] and only_a["keys"] == ["a"]
