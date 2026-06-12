"""Multi-agent coordination: blackboard, registry, event log, hooks, DAG scheduler.

State lives in <work_dir>/.delegate/:
  board.json     shared blackboard (agents publish results here)
  registry.json  live roster: who exists, their task, status, files
  events.jsonl   append-only lifecycle log
All read-modify-write goes through store.LOCK.
"""

import json
import time
import shlex
import shutil
import subprocess

from store import LOCK, coord_file, read_json
from sandbox import check_command


# ------------------------- blackboard -------------------------


def board_get(work: str, key: str = None):
    """Read the shared blackboard (whole dict, or one key)."""
    with LOCK:
        b = read_json(coord_file(work, "board.json"), {})
    return b if key is None else b.get(key)


def board_set(work: str, key: str, value, agent: str = None) -> bool:
    """Publish a value to the shared blackboard under `key`."""
    p = coord_file(work, "board.json")
    with LOCK:
        p.parent.mkdir(parents=True, exist_ok=True)
        b = read_json(p, {})
        b[key] = value
        p.write_text(json.dumps(b, indent=2))
    event(work, "board_set", agent or "?", key=key)
    return True


def board_append(work: str, key: str, item, agent: str = None) -> int:
    """Append to a list-valued board key (resets to a list if it wasn't one).

    Used for accumulating feeds like human<->agent messages, where overwrite is
    wrong. Returns the new length.
    """
    p = coord_file(work, "board.json")
    with LOCK:
        p.parent.mkdir(parents=True, exist_ok=True)
        b = read_json(p, {})
        lst = b.get(key)
        if not isinstance(lst, list):
            lst = []
        lst.append(item)
        b[key] = lst
        p.write_text(json.dumps(b, indent=2))
    event(work, "board_append", agent or "?", key=key)
    return len(lst)


# ------------------------- work claims (anti-overlap) -------------------------


def claim_work(work: str, agent: str, items: list) -> dict:
    """Atomically lease work units so two agents never take the same one.

    Records each requested item under claims.json -> {item: agent}. Returns
    {granted: [...], taken: {item: other_agent}}. An item already claimed by THIS
    agent is re-granted (idempotent). The whole check-and-set is under LOCK, so a
    fleet of agents (Claude sub-agents OR cheap workers) fanning out over the same
    work_dir can call this to partition the work with no double-coverage.
    """
    p = coord_file(work, "claims.json")
    granted, taken = [], {}
    with LOCK:
        p.parent.mkdir(parents=True, exist_ok=True)
        claims = read_json(p, {})
        for it in items:
            key = str(it)
            owner = claims.get(key)
            if owner is None or owner == agent:
                claims[key] = agent
                granted.append(it)
            else:
                taken[key] = owner
        p.write_text(json.dumps(claims, indent=2))
    if granted:
        event(work, "claim", agent, count=len(granted))
    return {"granted": granted, "taken": taken}


def release_work(work: str, agent: str, items: list = None) -> int:
    """Release this agent's claims (all of them, or just `items`). Returns the
    count released — lets a re-planned fleet hand units back for reassignment."""
    p = coord_file(work, "claims.json")
    n = 0
    with LOCK:
        claims = read_json(p, {})
        want = set(map(str, items)) if items is not None else None
        for key in [k for k, v in claims.items() if v == agent]:
            if want is None or key in want:
                del claims[key]
                n += 1
        if p.exists():
            p.write_text(json.dumps(claims, indent=2))
    return n


# ------------------------- registry -------------------------


def reg_get(work: str) -> dict:
    """Read the agent registry."""
    with LOCK:
        return read_json(coord_file(work, "registry.json"), {})


def reg_update(work: str, agent_id: str, **fields) -> None:
    """Create/update an agent's registry entry."""
    p = coord_file(work, "registry.json")
    with LOCK:
        p.parent.mkdir(parents=True, exist_ok=True)
        r = read_json(p, {})
        cur = r.get(agent_id, {})
        cur.update(fields)
        r[agent_id] = cur
        p.write_text(json.dumps(r, indent=2))


# ------------------------- events -------------------------


def event(work: str, etype: str, agent_id: str, **data) -> None:
    """Append one lifecycle event to events.jsonl."""
    p = coord_file(work, "events.jsonl")
    rec = {"ts": round(time.time(), 3), "type": etype, "agent": agent_id, **data}
    with LOCK:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")


def events_tail(work: str, limit: int = 50) -> list:
    p = coord_file(work, "events.jsonl")
    if not p.exists():
        return []
    with LOCK:
        lines = p.read_text(encoding="utf-8").splitlines()
    out = []
    for ln in lines[-limit:]:
        if ln.strip():
            try:
                out.append(json.loads(ln))
            except Exception:  # noqa: BLE001
                pass
    return out


def aggregate_board(work: str, keys: list = None, dedup: bool = True) -> dict:
    """Collapse the board into one digest for the parent — the N-reports-to-1
    context saver for a fan-out fleet.

    Each agent publishes its result under its own board key; this merges them.
    List-valued entries are concatenated (deduplicated by JSON identity when
    `dedup`); scalar/dict entries are returned per key. `keys` limits which board
    keys are folded in (default: all). Returns {items: [...], by_key: {...},
    keys: [...], n_sources: int} so the orchestrator reads ONE merged result
    instead of pulling every agent's full report into its context.
    """
    board = board_get(work) or {}
    sel = [k for k in board if (keys is None or k in keys)]
    items, by_key, seen = [], {}, set()
    for k in sel:
        v = board[k]
        by_key[k] = v
        vals = v if isinstance(v, list) else [v]
        for item in vals:
            if dedup:
                sig = json.dumps(item, sort_keys=True, default=str)
                if sig in seen:
                    continue
                seen.add(sig)
            items.append(item)
    return {"items": items, "by_key": by_key, "keys": sel, "n_sources": len(sel)}


def coord_clear(work: str) -> None:
    """Wipe board/registry/events for a fresh coordinated run (keeps the ledger)."""
    with LOCK:
        for name in (
            "board.json",
            "registry.json",
            "events.jsonl",
            "messages.jsonl",
            "toolcalls.jsonl",
            "toolcalls.jsonl.1",
            "claims.json",
        ):
            f = coord_file(work, name)
            if f.exists():
                f.unlink()
        agents_dir = coord_file(work, "agents")
        if agents_dir.is_dir():
            shutil.rmtree(agents_dir, ignore_errors=True)


# ------------------------- hooks -------------------------


def run_hook(work: str, hooks: dict, name: str, ctx: dict, allow_cmds: list):
    """Run a caller-supplied lifecycle shell hook, gated like every other command.

    hooks[name] is a template; {id}/{status}/{output_path}/{error} are substituted
    (shell-quoted). Returns a short result string, or None if no such hook.
    """
    if not hooks:
        return None
    tmpl = hooks.get(name)
    if not tmpl:
        return None
    cmd = tmpl
    for k, v in ctx.items():
        cmd = cmd.replace("{" + k + "}", shlex.quote(str(v if v is not None else "")))
    aid = ctx.get("id", "?")
    denied = check_command(cmd, allow_cmds or [])
    if denied:
        event(work, "hook_denied", aid, hook=name, reason=denied[:200])
        return f"denied ({denied})"
    try:
        r = subprocess.run(
            cmd, shell=True, cwd=work, capture_output=True, text=True, timeout=120
        )
        out = f"exit={r.returncode} {(r.stdout + r.stderr)[-300:]}"
    except Exception as e:  # noqa: BLE001
        out = f"hook error: {type(e).__name__}: {e}"
    event(work, "hook_ran", aid, hook=name, result=out[:200])
    return out


# ------------------------- DAG scheduler -------------------------


def plan_ready(pending: set, by_id: dict, results: dict):
    """Pure DAG step. Given the still-pending ids, the order map, and finished
    results, return (ready_to_run, to_skip) where to_skip is [(id, failed_deps)].

    An order is ready when all its depends_on are no longer pending. If any of
    those finished deps failed/were skipped, the order is skipped instead.
    Unknown dep ids (never in the batch) are treated as satisfied.
    """
    ready, skip = [], []
    for oid in pending:
        deps = by_id.get(oid, {}).get("depends_on") or []
        if any(d in pending for d in deps):
            continue  # a dependency hasn't finished yet
        bad = [
            d for d in deps if results.get(d, {}).get("status") in ("failed", "skipped")
        ]
        if bad:
            skip.append((oid, bad))
        else:
            ready.append(oid)
    return ready, skip
