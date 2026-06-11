"""Tests for context compaction: segmentation, estimation, and the compactor."""

import sys
import asyncio
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import compaction  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# ------------------------- pure helpers -------------------------


def test_estimate_tokens_grows():
    small = compaction.estimate_tokens([{"role": "user", "content": "hi"}])
    big = compaction.estimate_tokens([{"role": "user", "content": "x" * 4000}])
    assert big > small


def test_split_segments_groups_tool_responses():
    msgs = [
        {"role": "assistant", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": "r1"},
        {"role": "assistant", "tool_calls": [{"id": "2"}]},
        {"role": "tool", "tool_call_id": "2", "content": "r2"},
    ]
    segs = compaction.split_segments(msgs)
    assert len(segs) == 2
    assert len(segs[0]) == 2 and segs[0][0]["role"] == "assistant"


def _convo(n):
    """system + task + n assistant/tool turns of bulky content."""
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do the task"},
    ]
    for i in range(n):
        msgs.append(
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": str(i), "function": {"name": "read_file", "arguments": "{}"}}
                ],
            }
        )
        msgs.append({"role": "tool", "tool_call_id": str(i), "content": "x" * 500})
    return msgs


# ------------------------- maybe_compact -------------------------


def test_under_budget_no_compaction():
    msgs = _convo(2)
    out, info = run(
        compaction.maybe_compact(None, msgs, "m", budget=10_000, keep_segments=4)
    )
    assert info is None
    assert out is msgs


def test_compacts_when_over_budget(monkeypatch):
    async def fake_call(*a, **k):
        return {"text": "SUMMARY", "model": "m", "usage": {"prompt_tokens": 5}}

    monkeypatch.setattr(compaction, "call_model", fake_call)

    msgs = _convo(10)  # 20 body messages, well over a tiny budget
    out, info = run(
        compaction.maybe_compact(None, msgs, "m", budget=50, keep_segments=3)
    )
    assert info is not None
    assert info["segments_compacted"] == 7  # 10 turns - 3 kept
    # head (system+task) + summary note + 3 kept turns (2 msgs each)
    assert out[0]["role"] == "system"
    assert out[1]["content"] == "do the task"
    assert "[CONTEXT COMPACTED" in out[2]["content"]
    assert "SUMMARY" in out[2]["content"]
    assert len(out) < len(msgs)
    # tool pairing preserved: the first kept turn starts with an assistant msg
    assert out[3]["role"] == "assistant"


def test_not_enough_segments_to_compact(monkeypatch):
    async def fake_call(*a, **k):  # should not be reached
        raise AssertionError("should not summarize")

    monkeypatch.setattr(compaction, "call_model", fake_call)

    msgs = _convo(3)  # only 3 turns, keep_segments=4 -> nothing safe to drop
    out, info = run(
        compaction.maybe_compact(None, msgs, "m", budget=1, keep_segments=4)
    )
    assert info is None
