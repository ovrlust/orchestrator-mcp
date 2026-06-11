"""A small message bus for agent<->agent and human/orchestrator<->agent comms.

Stored append-only in <work_dir>/.delegate/messages.jsonl, one JSON per line:
  {seq, ts, from, to, text}        to="" means broadcast.

Beyond the shared blackboard (publish facts), this gives DIRECTED messages and a
read-since cursor so an agent can ask another agent something, or the orchestrator
can steer a running agent, and each side can pull only what's new for it.
"""

import json
import time

from store import LOCK, coord_file
from coordination import event


def _last_seq(p) -> int:
    """Highest seq already in the file (0 if none). Counting lines would drift
    on a corrupt/partial line and break every read-since cursor."""
    if not p.exists():
        return 0
    last = 0
    for ln in p.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            try:
                last = max(last, int(json.loads(ln).get("seq", 0)))
            except Exception:  # noqa: BLE001 - skip torn lines
                pass
    return last


def post_message(work: str, frm: str, text: str, to: str = "") -> int:
    """Append a message (to='' = broadcast to everyone). Returns its seq number."""
    p = coord_file(work, "messages.jsonl")
    with LOCK:
        p.parent.mkdir(parents=True, exist_ok=True)
        seq = _last_seq(p) + 1
        rec = {
            "seq": seq,
            "ts": round(time.time(), 3),
            "from": frm,
            "to": to,
            "text": text,
        }
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    event(work, "message", frm, to=to or "all")
    return seq


def _load(work: str) -> list:
    p = coord_file(work, "messages.jsonl")
    if not p.exists():
        return []
    with LOCK:
        lines = p.read_text(encoding="utf-8").splitlines()
    out = []
    for ln in lines:
        if ln.strip():
            try:
                out.append(json.loads(ln))
            except Exception:  # noqa: BLE001
                pass
    return out


def read_messages(work: str, agent: str = "", since: int = 0) -> list:
    """Messages with seq > `since`. For a named `agent`, return broadcasts plus
    messages addressed to it (and its own). agent='' (orchestrator/human/viewer)
    sees everything."""
    res = []
    for m in _load(work):
        if m.get("seq", 0) <= since:
            continue
        to = m.get("to") or ""
        if agent and to and to != agent and m.get("from") != agent:
            continue  # directed to someone else
        res.append(m)
    return res
