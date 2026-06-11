"""Unit tests for the network-free logic: pricing, ledger, validators, sandbox."""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import ledger  # noqa: E402
import sandbox  # noqa: E402
import validators  # noqa: E402


# ------------------------- pricing -------------------------


def test_cost_usd_known_model():
    # gpt-4o-mini: $0.15 in / $0.60 out per 1M
    usd = ledger.cost_usd(
        "openai/gpt-4o-mini",
        {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
    )
    assert usd == round(0.15 + 0.60, 6)


def test_cost_usd_unknown_model_is_zero():
    assert (
        ledger.cost_usd(
            "nope/unknown", {"prompt_tokens": 999, "completion_tokens": 999}
        )
        == 0.0
    )


def test_cost_usd_missing_usage_fields():
    assert ledger.cost_usd("openai/gpt-4o-mini", {}) == 0.0


# ------------------------- ledger -------------------------


def test_record_and_summary(tmp_path):
    w = str(tmp_path)
    ledger.record_spend(
        w, "openai/gpt-4o-mini", {"prompt_tokens": 1_000_000, "completion_tokens": 0}
    )
    ledger.record_spend(
        w, "openai/gpt-4o-mini", {"prompt_tokens": 0, "completion_tokens": 1_000_000}
    )
    s = ledger.spend_summary(w)
    assert s["calls"] == 2
    assert s["usd"] == round(0.15 + 0.60, 6)
    assert s["prompt_tokens"] == 1_000_000
    assert s["by_model"]["openai/gpt-4o-mini"]["calls"] == 2


def test_summary_empty(tmp_path):
    s = ledger.spend_summary(str(tmp_path))
    assert s["calls"] == 0 and s["usd"] == 0.0


def test_summary_corrupt_ledger(tmp_path):
    p = ledger.ledger_path(str(tmp_path))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    assert "error" in ledger.spend_summary(str(tmp_path))


# ------------------------- sandbox -------------------------


def test_safe_allows_inside(tmp_path):
    assert sandbox.safe_path(str(tmp_path), "a/b.txt").name == "b.txt"


def test_safe_rejects_escape(tmp_path):
    try:
        sandbox.safe_path(str(tmp_path), "../../etc/passwd")
        assert False, "should have raised"
    except ValueError:
        pass


# ------------------------- validators -------------------------


def v(spec, text, work=".", allow=None):
    return validators.validate(spec, text, None, work, allow or [])


def test_validate_none_passes():
    assert v(None, "anything")["ok"]


def test_nonempty():
    assert v({"type": "nonempty"}, "hello")["ok"]
    assert not v({"type": "nonempty"}, "   ")["ok"]


def test_nonempty_bounds():
    assert not v({"type": "nonempty", "min_len": 5}, "hi")["ok"]
    assert not v({"type": "nonempty", "max_len": 3}, "toolong")["ok"]


def test_nonempty_refusal():
    assert not v({"type": "nonempty"}, "I cannot help with that")["ok"]


def test_regex_required():
    assert v({"type": "regex", "pattern": r"\d+"}, "abc123")["ok"]
    assert not v({"type": "regex", "pattern": r"\d+"}, "abc")["ok"]


def test_regex_must_not():
    assert not v({"type": "regex", "pattern": "TODO", "must_not": True}, "x TODO y")[
        "ok"
    ]
    assert v({"type": "regex", "pattern": "TODO", "must_not": True}, "clean")["ok"]


def test_json_valid_and_invalid():
    assert v({"type": "json"}, '{"a": 1}')["ok"]
    assert not v({"type": "json"}, "{not json")["ok"]


def test_json_schema():
    spec = {"type": "json", "schema": {"type": "object", "required": ["a"]}}
    assert v(spec, '{"a": 1}')["ok"]
    assert not v(spec, '{"b": 1}')["ok"]


def test_shell_requires_allowlist(tmp_path):
    r = v({"type": "shell", "cmd": "echo hi"}, "", str(tmp_path), allow=[])
    assert not r["ok"] and "allow_commands" in r["error"]


def test_shell_denylist(tmp_path):
    r = v({"type": "shell", "cmd": "rm -rf /"}, "", str(tmp_path), allow=["rm"])
    assert not r["ok"] and "denied" in r["error"].lower()


def test_shell_passes_on_exit_zero(tmp_path):
    r = v({"type": "shell", "cmd": "echo ok"}, "", str(tmp_path), allow=["echo"])
    assert r["ok"]


def test_shell_fails_on_nonzero(tmp_path):
    r = v({"type": "shell", "cmd": "false"}, "", str(tmp_path), allow=["false"])
    assert not r["ok"]


def test_unknown_validator():
    assert not v({"type": "bogus"}, "x")["ok"]
