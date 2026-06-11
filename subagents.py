"""Sub-agent lifecycle on top of the agent loop: persistence, background spawn,
and resume — the orchestrator-facing equivalents of Claude's Agent/SendMessage.

Every run's full transcript is saved to <work_dir>/.delegate/agents/<id>.json,
so a finished agent can be continued later with its context intact (`send`).
`spawn` runs an agent as a background asyncio task in the MCP server process;
`result` polls or waits for it. Live progress is visible through the existing
registry heartbeats (monitor/agents tools).
"""

import re
import json
import time
import asyncio

from store import LOCK, coord_file
from coordination import reg_get
import messages as msgbus
import agent as agent_mod

# Background tasks need strong refs (the loop keeps only weak ones); keyed by
# (work_dir, agent_id) so two work_dirs can reuse an id without colliding.
_TASKS: dict = {}

_ID_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def valid_id(agent_id: str) -> bool:
    """agent_id becomes a filename — reject anything that could escape agents/."""
    return bool(_ID_RX.match(agent_id or ""))


def _path(work: str, agent_id: str):
    return coord_file(work, f"agents/{agent_id}.json")


def save(work: str, agent_id: str, record: dict) -> None:
    p = _path(work, agent_id)
    record["updated"] = round(time.time(), 3)
    with LOCK:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(record), encoding="utf-8")


def load(work: str, agent_id: str):
    p = _path(work, agent_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - corrupt record == no record
        return None


def _status_of(r: dict) -> str:
    if "error" in r:
        return "failed"
    if str(r.get("result", "")).startswith("(max_steps"):
        return "incomplete"
    return "done"


async def run_and_persist(
    work: str,
    task: str,
    model: str,
    agent_id: str,
    allow_cmds: list,
    max_steps: int,
    system: str,
    agent_type: str,
    output_schema: dict = None,
    messages: list = None,
) -> dict:
    """Run the agent loop, persist the full transcript, return the result
    (without the transcript — that stays on disk)."""
    r = await agent_mod.run_agent_loop(
        task,
        work,
        model,
        agent_id,
        allow_cmds,
        max_steps,
        system,
        agent_type=agent_type,
        output_schema=output_schema,
        messages=messages,
    )
    transcript = r.pop("messages", None)
    save(
        work,
        agent_id,
        {
            "agent_id": agent_id,
            "agent_type": agent_type,
            "model": model,
            "allow_cmds": allow_cmds or [],
            "output_schema": output_schema,
            "task": (task or "(resumed)")[:300],
            "status": _status_of(r),
            "result": r,
            "messages": transcript,
        },
    )
    return r


def spawn(
    work: str,
    task: str,
    model: str,
    agent_id: str,
    allow_cmds: list,
    max_steps: int,
    system: str,
    agent_type: str,
    output_schema: dict = None,
) -> dict:
    """Start an agent in the background. Returns immediately with its id."""
    key = (work, agent_id)
    t = _TASKS.get(key)
    if t and not t.done():
        return {"error": f"agent '{agent_id}' is already running in this work_dir"}
    # Marker record so `result`/`send` can tell "running" from "never existed",
    # and detect runs orphaned by a server restart.
    save(
        work,
        agent_id,
        {
            "agent_id": agent_id,
            "agent_type": agent_type,
            "model": model,
            "allow_cmds": allow_cmds or [],
            "output_schema": output_schema,
            "task": task[:300],
            "status": "running",
            "result": None,
            "messages": None,
        },
    )
    _TASKS[key] = asyncio.create_task(
        run_and_persist(
            work,
            task,
            model,
            agent_id,
            allow_cmds,
            max_steps,
            system,
            agent_type,
            output_schema,
        )
    )
    return {
        "agent_id": agent_id,
        "status": "running",
        "note": "running in background — agent_result(work_dir, agent_id) to collect, "
        "monitor(work_dir) to watch, agent_send to steer",
    }


async def result(work: str, agent_id: str, wait_seconds: float = 0) -> dict:
    """Poll (or wait up to wait_seconds for) a spawned agent's result."""
    key = (work, agent_id)
    t = _TASKS.get(key)
    if t:
        if wait_seconds > 0 and not t.done():
            try:
                await asyncio.wait_for(asyncio.shield(t), wait_seconds)
            except asyncio.TimeoutError:
                pass
        if not t.done():
            reg = reg_get(work).get(agent_id, {})
            return {
                "status": "running",
                "step": reg.get("step"),
                "last_active": reg.get("last_active"),
                "files_so_far": reg.get("files", []),
            }
        _TASKS.pop(key, None)
        try:
            r = t.result()
        except Exception as e:  # noqa: BLE001 - surface the crash, don't raise
            return {"status": "failed", "error": f"{type(e).__name__}: {e}"}
        return {"status": _status_of(r), **r}
    rec = load(work, agent_id)
    if not rec:
        return {"error": f"no agent '{agent_id}' in this work_dir"}
    if rec.get("status") == "running":
        return {
            "status": "lost",
            "error": "agent was running but the server restarted mid-run; "
            "its transcript was not saved — spawn it again",
        }
    return {"status": rec.get("status"), **(rec.get("result") or {})}


async def send(work: str, agent_id: str, message: str, max_steps: int = 15) -> dict:
    """Continue an agent with a follow-up message, full context intact.

    Running agent: the message is posted to the live bus (push delivery injects
    it into the loop). Finished agent: its saved transcript is reloaded, the
    message appended, and the loop resumed with the SAME model/allowlist/type.
    """
    key = (work, agent_id)
    t = _TASKS.get(key)
    if t and not t.done():
        seq = msgbus.post_message(work, "orchestrator", message, agent_id)
        return {
            "status": "delivered_live",
            "seq": seq,
            "note": "agent is mid-run; message will be pushed into its loop within a step",
        }
    rec = load(work, agent_id)
    if not rec:
        return {"error": f"no agent '{agent_id}' in this work_dir"}
    transcript = rec.get("messages")
    if not transcript:
        return {
            "error": f"agent '{agent_id}' has no saved transcript (lost run?) — "
            "spawn a new agent instead"
        }
    transcript = transcript + [{"role": "user", "content": message}]
    return await run_and_persist(
        work,
        "",
        rec.get("model", ""),
        agent_id,
        rec.get("allow_cmds") or [],
        max_steps,
        "",
        rec.get("agent_type", "general"),
        rec.get("output_schema"),
        messages=transcript,
    )
