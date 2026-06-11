"""Regression tests for the review fixes: command-gate hardening, transitive
dependency skips, ledger corruption safety, and message seq robustness."""

import sys
import json
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import ledger  # noqa: E402
import sandbox  # noqa: E402
import messages as msgbus  # noqa: E402
import coordination as coord  # noqa: E402
import validators  # noqa: E402


# ------------------------- check_command -------------------------


def test_check_command_allows_exact_and_prefixed():
    assert sandbox.check_command("echo hi", ["echo"]) is None
    assert sandbox.check_command("pytest", ["pytest"]) is None
    assert sandbox.check_command("git status --short", ["git status"]) is None


def test_check_command_rejects_token_smuggling():
    # "echofoo" must not pass allow=["echo"]
    assert sandbox.check_command("echofoo", ["echo"]) is not None


def test_check_command_rejects_shell_chaining():
    for cmd in (
        "echo x; touch pwned",
        "echo x && touch pwned",
        "echo x | sh",
        "echo `touch pwned`",
        "echo $(touch pwned)",
        "echo x > important.txt",
    ):
        assert sandbox.check_command(cmd, ["echo"]) is not None, cmd


def test_check_command_empty_allowlist_denies():
    assert sandbox.check_command("echo hi", []) is not None


def test_deny_catches_more_rm_forms():
    assert sandbox.DENY.search("rm --recursive --force x")
    assert sandbox.DENY.search("find . -exec rm {} +")
    assert sandbox.DENY.search("find . -name '*.tmp' -delete")


def test_shell_validator_uses_hardened_gate(tmp_path):
    v = validators.validate(
        {"type": "shell", "cmd": "echo ok; touch pwned"},
        "out",
        None,
        str(tmp_path),
        ["echo"],
    )
    assert not v["ok"]
    assert not (tmp_path / "pwned").exists()


def test_hook_uses_hardened_gate(tmp_path):
    out = coord.run_hook(
        str(tmp_path),
        {"on_finish": "echo hi && touch pwned"},
        "on_finish",
        {"id": "a1"},
        ["echo"],
    )
    assert "denied" in out
    assert not (tmp_path / "pwned").exists()


# ------------------------- DAG scheduler: transitive skips -------------------------


def test_transitive_dep_failure_is_not_a_cycle():
    # A failed; B depends on A; C depends on B. C must be skipped for failed
    # deps (transitively), not mislabeled as a dependency cycle.
    by_id = {
        "b": {"id": "b", "depends_on": ["a"]},
        "c": {"id": "c", "depends_on": ["b"]},
    }
    results = {"a": {"id": "a", "status": "failed"}}
    pending = {"b", "c"}

    ready, skip = coord.plan_ready(pending, by_id, results)
    assert ready == [] and skip == [("b", ["a"])]
    results["b"] = {"id": "b", "status": "skipped"}
    pending.discard("b")

    ready, skip = coord.plan_ready(pending, by_id, results)
    assert ready == [] and skip == [("c", ["b"])]


# ------------------------- ledger corruption safety -------------------------


def test_record_spend_preserves_corrupt_ledger(tmp_path):
    p = ledger.ledger_path(str(tmp_path))
    p.parent.mkdir(parents=True)
    p.write_text("{ not json")
    ledger.record_spend(
        str(tmp_path),
        "openai/gpt-4o-mini",
        {"prompt_tokens": 10, "completion_tokens": 10},
    )
    # The corrupt history is set aside, not silently destroyed.
    assert p.with_suffix(".json.corrupt").read_text() == "{ not json"
    led = json.loads(p.read_text())
    assert len(led) == 1


# ------------------------- message seq -------------------------


def test_message_seq_survives_corrupt_line(tmp_path):
    w = str(tmp_path)
    msgbus.post_message(w, "a", "one")
    msgbus.post_message(w, "a", "two")
    p = coord.coord_file(w, "messages.jsonl")
    with p.open("a", encoding="utf-8") as f:
        f.write("{torn line\n")
    seq = msgbus.post_message(w, "a", "three")
    assert seq == 3  # max(seq)+1, not line-count+1 (which would collide at 4)
    seen = [m["seq"] for m in msgbus.read_messages(w)]
    assert seen == [1, 2, 3]
