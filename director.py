"""Director + Supervisor: split a plan into sections, each run by its OWN autonomous
agent, in parallel, respecting dependencies. This is what activates agent<->agent
comms (push delivery) — you need >=2 run_agent loops live at once for them to talk.

Two modes, same dispatch core:
  Director  (fire-and-forget) — dispatch the ready agents, await the wave, repeat.
            Cheapest: the orchestrator (Claude) is not in the loop while they run.
  Supervisor(live watch)      — while a wave runs, a supervisor MODEL polls the live
            state every poll_interval and may message a struggling agent or stop the
            run. More reliable, more expensive (each poll is a model call).

A section:
  id           label (registry/board/report key)
  task         the instruction for that agent
  model?       worker model override
  depends_on?  [id,...] run after these; their published results are injected
  allow_commands? per-section shell allowlist (else the run-wide one)
  max_steps?   agent step cap (default 25)
  system?      system prompt override
  validate?    optional deterministic gate run AFTER the agent finishes
               ({type: shell|json|regex|nonempty,...}); failing it marks the
               section failed (no rollback — agents touch many files).
"""

import os
import json
import asyncio
import pathlib

import httpx

import workers
from workers import call_model
from coordination import (
    reg_update,
    reg_get,
    event,
    events_tail,
    board_get,
    board_set,
    coord_clear,
    plan_ready,
)
from messages import post_message, read_messages
from validators import validate
from delegate import awareness_block, preflight
from agent import run_agent_loop


def _status_of(r: dict) -> str:
    """Map a run_agent_loop return to applied/failed/incomplete."""
    if "error" in r:
        return "failed"
    if str(r.get("result", "")).startswith("(max_steps"):
        return "incomplete"
    return "done"


async def run_section(sec: dict, work: str, model: str, allow_commands) -> dict:
    """Run one section as its own agent; publish its result to the board for deps."""
    oid = sec["id"]
    deps = sec.get("depends_on") or []
    try:
        task = awareness_block(work, deps) + sec.get("task", "")
        r = await run_agent_loop(
            task,
            work,
            sec.get("model") or model,
            oid,
            sec.get("allow_commands") or allow_commands or [],
            int(sec.get("max_steps", 25)),
            sec.get("system", ""),
        )
        status = _status_of(r)
        spec = sec.get("validate")
        if status == "done" and spec:
            v = await asyncio.to_thread(
                validate,
                spec,
                str(r.get("result", "")),
                None,
                work,
                allow_commands or [],
            )
            if not v["ok"]:
                status = "failed"
                r["validate_error"] = v["error"]
        board_set(
            work,
            oid,
            {
                "status": status,
                "result": str(r.get("result", ""))[:2000],
                "files": r.get("files_changed", []),
            },
            agent=oid,
        )
        out = {
            "id": oid,
            "status": status,
            "result": r.get("result") or r.get("error", ""),
            "files_changed": r.get("files_changed", []),
            "usage": r.get("usage", {}),
        }
        if "error" in r:
            out["error"] = r["error"]
        if "validate_error" in r:
            out["error"] = r["validate_error"]
        return out
    except Exception as e:  # noqa: BLE001 — never let one agent crash the wave
        reg_update(work, oid, status="failed", error=str(e)[:200])
        event(work, "fail", oid, error=str(e)[:200])
        return {"id": oid, "status": "failed", "error": f"{type(e).__name__}: {e}"}


def _snapshot(work: str) -> dict:
    board = board_get(work) or {}
    return {
        "agents": reg_get(work),
        "events": events_tail(work, 20),
        "board_keys": sorted(board.keys()),
        "messages": read_messages(work, "", 0)[-15:],
    }


async def _supervisor_decide(client, snap: dict, model: str) -> dict:
    """One supervisor poll: look at live state, decide interventions. Returns
    {messages:[{to,text}], stop:bool, note:str}."""
    prompt = (
        "You supervise autonomous agents working in one repo. LIVE STATE:\n"
        + json.dumps(snap, default=str)[:6000]
        + "\n\nIf an agent looks stuck, looping, erroring, or off-track, intervene by "
        "messaging it. Respond with ONLY JSON: "
        '{"messages":[{"to":"agent_id or empty=broadcast","text":"guidance"}],'
        '"stop":false,"note":"one-line status"}. '
        "All healthy -> empty messages, stop:false."
    )
    r = await call_model(client, prompt, model, temperature=0)
    if "error" in r:
        return {
            "messages": [],
            "stop": False,
            "note": "supervisor error: " + r["error"],
        }
    txt = r.get("text", "")
    try:
        obj = json.loads(txt[txt.find("{") : txt.rfind("}") + 1])
        if not isinstance(obj, dict):
            raise ValueError
    except Exception:  # noqa: BLE001
        return {"messages": [], "stop": False, "note": txt[:200]}
    obj.setdefault("messages", [])
    obj.setdefault("stop", False)
    return obj


async def _supervised_wave(
    client, ready_pairs, work, supervisor_model, max_polls, poll_interval
):
    """Run a wave of (id, coro) under a polling supervisor.
    Returns (results, log, stopped)."""
    tasks = {oid: asyncio.create_task(coro) for oid, coro in ready_pairs}
    log = []
    polls = 0
    stopped = False
    while polls < max_polls:
        decision = await _supervisor_decide(client, _snapshot(work), supervisor_model)
        log.append(decision)
        msgs = decision.get("messages") or []
        for m in msgs if isinstance(msgs, list) else []:
            if isinstance(m, dict) and m.get("text"):
                post_message(work, "supervisor", m["text"], m.get("to", "") or "")
        if decision.get("stop"):
            stopped = True
            for t in tasks.values():
                t.cancel()
            break
        if all(t.done() for t in tasks.values()):
            break
        await asyncio.sleep(poll_interval)
        polls += 1
    gathered = await asyncio.gather(*tasks.values(), return_exceptions=True)
    results = []
    for (oid, _), r in zip(ready_pairs, gathered):
        if isinstance(r, BaseException):  # cancelled or crashed
            reg_update(work, oid, status="failed", error=str(r)[:200])
            results.append(
                {"id": oid, "status": "failed", "error": f"{type(r).__name__}: {r}"}
            )
        else:
            results.append(r)
    return results, log, stopped


async def run_director(
    sections: list,
    work_dir: str,
    model: str = "",
    allow_commands: list = None,
    reset: bool = False,
    supervise: bool = False,
    supervisor_model: str = "",
    max_polls: int = 20,
    poll_interval: float = 2.0,
) -> dict:
    """Split a plan into sections, each run by its own agent in parallel (deps
    respected). supervise=True adds a polling supervisor model. See server.direct /
    server.supervise for the contract."""
    err = preflight(sections)
    if err:
        return {"error": err}
    if not workers.API_KEY:
        return {"error": "OPENROUTER_API_KEY is not set in this server's env."}
    work = str(pathlib.Path(work_dir).expanduser().resolve())
    if not os.path.isdir(work):
        return {"error": f"work_dir not found: {work}"}
    if reset:
        coord_clear(work)

    by_id = {s["id"]: s for s in sections}
    for s in sections:
        reg_update(
            work,
            s["id"],
            task=s.get("task", "")[:120],
            status="pending",
            depends_on=s.get("depends_on") or [],
            kind="agent",
        )

    results: dict = {}
    pending = set(by_id)
    sup_log = []
    async with httpx.AsyncClient() as client:
        while pending:
            ready, skip = plan_ready(pending, by_id, results)
            for oid, bad in skip:
                results[oid] = {
                    "id": oid,
                    "status": "skipped",
                    "reason": f"dependencies failed: {bad}",
                }
                reg_update(work, oid, status="skipped", error=f"deps failed: {bad}")
                event(work, "skip", oid, failed_deps=bad)
                pending.discard(oid)
            if not ready:
                if skip:
                    continue  # skips may unblock transitive skips next pass
                if pending:  # nothing runnable, nothing skipped -> cycle/missing dep
                    for oid in list(pending):
                        results[oid] = {
                            "id": oid,
                            "status": "skipped",
                            "reason": "dependency cycle or missing dep",
                        }
                        reg_update(
                            work, oid, status="skipped", error="dependency cycle"
                        )
                        event(work, "skip", oid, reason="cycle")
                        pending.discard(oid)
                continue
            pairs = [
                (oid, run_section(by_id[oid], work, model, allow_commands))
                for oid in ready
            ]
            if supervise:
                batch, log, stopped = await _supervised_wave(
                    client,
                    pairs,
                    work,
                    supervisor_model or model,
                    max_polls,
                    poll_interval,
                )
                sup_log += log
            else:
                batch = await asyncio.gather(*[c for _, c in pairs])
                stopped = False
            for r in batch:
                results[r["id"]] = r
                pending.discard(r["id"])
            if stopped:
                # Supervisor said stop: don't dispatch the remaining waves.
                for oid in list(pending):
                    results[oid] = {
                        "id": oid,
                        "status": "skipped",
                        "reason": "stopped by supervisor",
                    }
                    reg_update(work, oid, status="skipped", error="supervisor stop")
                    event(work, "skip", oid, reason="supervisor_stop")
                    pending.discard(oid)
                break

    ordered = [results[s["id"]] for s in sections]
    done = [r for r in ordered if r["status"] == "done"]
    failed = [r for r in ordered if r["status"] == "failed"]
    skipped = [r for r in ordered if r["status"] == "skipped"]
    out = {
        "summary": {
            "total": len(ordered),
            "done": len(done),
            "failed": len(failed),
            "skipped": len(skipped),
            "failed_ids": [r["id"] for r in failed],
            "skipped_ids": [r["id"] for r in skipped],
        },
        "sections": ordered,
        "monitor": _snapshot(work),
    }
    if supervise:
        out["supervision"] = sup_log
    return out
