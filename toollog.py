"""Durable per-tool-call log for the autonomous worker (run_agent).

Why separate from events.jsonl: that log is coarse lifecycle (start/finish/fail).
This one is the fine-grained record of EVERY tool the worker invoked —
read_file, grep, edit_file, run_command, … — with truncated args, a result
preview, ok/err, and which step it happened on. Without it, an agent's actual
behavior vanishes the moment run_agent returns (only the last 12 calls come back
in transcript_tail). With it you can audit what a cheap worker did to your tree,
debug a bad run after the fact, and see every shell command it executed.

One JSONL line per call in <work_dir>/.delegate/toolcalls.jsonl, written under
the shared LOCK so concurrent agents don't interleave. Best-effort: a logging
failure never breaks the agent.
"""

import json
import time

from store import LOCK, coord_file

# Keep lines bounded — this is a log, not a data store.
ARGS_MAX = 400
RESULT_MAX = 600


def _trunc(v, n: int) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + f"…(+{len(s) - n})"


def log_call(work, agent_id, step, fn, args, result, ok=True) -> None:
    """Append one tool-call record. Never raises."""
    try:
        rec = {
            "ts": round(time.time(), 3),
            "agent": agent_id,
            "step": step,
            "fn": fn,
            "args": _trunc(args, ARGS_MAX),
            "result": _trunc(result, RESULT_MAX),
            "ok": bool(ok),
        }
        p = coord_file(work, "toolcalls.jsonl")
        with LOCK:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
    except Exception:  # noqa: BLE001 - logging must never break a run
        pass


def tail(work, limit=100, agent="", fn="", errors_only=False) -> list:
    """Read recent tool calls, newest-relevant filtered. agent/fn filter exactly;
    errors_only keeps only failed calls. Returns up to `limit` (most recent)."""
    p = coord_file(work, "toolcalls.jsonl")
    if not p.exists():
        return []
    with LOCK:
        lines = p.read_text(encoding="utf-8").splitlines()
    out = []
    for ln in lines:
        if not ln.strip():
            continue
        try:
            rec = json.loads(ln)
        except Exception:  # noqa: BLE001
            continue
        if agent and rec.get("agent") != agent:
            continue
        if fn and rec.get("fn") != fn:
            continue
        if errors_only and rec.get("ok", True):
            continue
        out.append(rec)
    return out[-limit:]
