"""Context compaction for the run_agent loop.

A cheap model's window is small; the agent transcript grows every step. When it
crosses a token budget we replace the OLDEST turns with a one-line summary and
keep the most recent turns verbatim. Compaction happens at turn boundaries so an
assistant tool_call is never split from its tool responses (which would make the
message list invalid).
"""

import json

from workers import call_model


def estimate_tokens(messages: list) -> int:
    """Cheap, dependency-free token estimate (~4 chars/token over the JSON)."""
    return sum(len(json.dumps(m, default=str)) for m in messages) // 4


def split_segments(msgs: list) -> list:
    """Group messages into turns: each assistant message starts a turn and owns
    the tool responses that follow it. Used so compaction keeps turns whole."""
    segs, cur = [], []
    for m in msgs:
        if m.get("role") == "assistant":
            if cur:
                segs.append(cur)
            cur = [m]
        else:
            cur.append(m)
    if cur:
        segs.append(cur)
    return segs


def _render(segments: list) -> str:
    """Flatten old turns into plain text for the summarizer."""
    out = []
    for seg in segments:
        for m in seg:
            role = m.get("role", "?")
            if m.get("tool_calls"):
                for c in m["tool_calls"]:
                    fn = c.get("function", {})
                    out.append(
                        f"[{role} call] {fn.get('name')} {str(fn.get('arguments'))[:200]}"
                    )
            content = m.get("content")
            if content:
                out.append(f"[{role}] {str(content)[:400]}")
    return "\n".join(out)


async def maybe_compact(
    client, messages: list, model: str, budget: int, keep_segments: int
):
    """If `messages` exceeds `budget` tokens, summarize all but the last
    `keep_segments` turns. Returns (new_messages, info) where info is None when
    no compaction happened, else {segments_compacted, model, usage}.

    Preserves messages[0:2] (system + original task) and the recent turns intact.
    """
    if estimate_tokens(messages) <= budget:
        return messages, None
    head, body = messages[:2], messages[2:]
    segs = split_segments(body)
    if len(segs) <= keep_segments:
        return messages, None  # nothing safe to drop

    old, recent = segs[:-keep_segments], segs[-keep_segments:]
    summary = await call_model(
        client,
        "Summarize the work done so far so it can continue without the full log. "
        "List: what was attempted, key findings, files changed, and what remains. "
        "Be terse.\n\n" + _render(old),
        model,
        "",
        0.0,
        400,
    )
    stext = summary.get("text") or "(summary unavailable)"
    note = {
        "role": "user",
        "content": "[CONTEXT COMPACTED — earlier steps summarized]\n" + stext,
    }
    new_messages = head + [note] + [m for seg in recent for m in seg]
    return new_messages, {
        "segments_compacted": len(old),
        "model": summary.get("model", model),
        "usage": summary.get("usage", {}),
    }
