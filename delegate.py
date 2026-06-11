"""The autonomous delegate loop: a DAG of orders, each worker -> apply ->
validate -> retry -> share, with lifecycle events and dependency awareness.
"""

import os
import json
import asyncio
import pathlib

import httpx

import workers
from workers import call_model
from ledger import record_spend
from coordination import (
    board_get,
    board_set,
    reg_get,
    reg_update,
    event,
    events_tail,
    coord_clear,
    run_hook,
    plan_ready,
)
from sandbox import safe_path
from validators import validate
from edits import apply_edits, parse_edit_payload, EditError


def awareness_block(work: str, deps: list) -> str:
    """Context prepended to a worker prompt so it sees the board + its deps' output."""
    board = board_get(work) or {}
    parts = []
    if board:
        parts.append(
            "SHARED BOARD (results other agents have published):\n"
            + json.dumps(board, indent=2)[:4000]
        )
    if deps:
        lines = []
        for d in deps:
            dv = board.get(d)
            lines.append(
                f"- {d}: "
                + (json.dumps(dv)[:800] if dv is not None else "(no published output)")
            )
        parts.append("OUTPUT FROM YOUR DEPENDENCIES:\n" + "\n".join(lines))
    return ("\n\n".join(parts) + "\n\n---\n\n") if parts else ""


def preflight(orders: list):
    """Validate the order set before running. Returns an error string, or None.

    Catches the mistakes that would otherwise silently misbehave: missing/duplicate
    ids and self-dependencies. (Cycles and missing deps are handled at run time by
    the scheduler, which skips them rather than hanging.)
    """
    if not isinstance(orders, list) or not orders:
        return "orders must be a non-empty list"
    ids = []
    for o in orders:
        oid = o.get("id")
        if not oid:
            return "every order needs an 'id'"
        ids.append(oid)
    dups = sorted({i for i in ids if ids.count(i) > 1})
    if dups:
        return f"duplicate order ids: {dups}"
    for o in orders:
        if o["id"] in (o.get("depends_on") or []):
            return f"order {o['id']} depends on itself"
    return None


async def process_order(
    client, order: dict, work: str, allow_cmds: list, hooks: dict
) -> dict:
    """One order: worker -> apply -> validate -> retry, with lifecycle + sharing."""
    oid = order.get("id", "?")
    prompt = order.get("prompt", "")
    model = order.get("model", "")
    system = order.get("system", "")
    temperature = float(order.get("temperature", 0.0))
    max_tokens = int(order.get("max_tokens", 0))
    spec = order.get("validate")
    out = order.get("output_path")
    deps = order.get("depends_on") or []
    share = bool(order.get("share", False))
    edit_mode = bool(order.get("edit", False))
    fallback = order.get("fallback", "")
    max_retries = int(order.get("max_retries", 1))

    reg_update(
        work, oid, task=prompt[:120], status="running", depends_on=deps, attempts=0
    )
    event(work, "start", oid, task=prompt[:80])
    run_hook(
        work,
        hooks,
        "on_start",
        {"id": oid, "status": "running", "output_path": out or ""},
        allow_cmds,
    )

    abspath = safe_path(work, out) if out else None
    original = abspath.read_bytes() if (abspath and abspath.exists()) else None

    attempts = 0
    last_err = None
    spent = 0.0
    base_prompt = awareness_block(work, deps) + prompt
    cur_prompt = base_prompt

    while attempts <= max_retries:
        attempts += 1
        r = await call_model(
            client, cur_prompt, model, system, temperature, max_tokens, fallback
        )
        if "error" in r:
            last_err = r["error"]
            cur_prompt = f"{base_prompt}\n\nThe previous attempt errored: {last_err}\nReturn ONLY the requested output."
            continue
        spent += record_spend(work, r["model"], r.get("usage", {}))
        text = r["text"]
        applied_content = text
        if edit_mode:
            if abspath is None or original is None:
                last_err = "edit mode requires an existing output_path file to edit"
                break
            try:
                ops = parse_edit_payload(text)
                applied_content = apply_edits(original.decode("utf-8", "replace"), ops)
            except EditError as e:
                last_err = str(e)
                cur_prompt = (
                    f"{base_prompt}\n\nYour edits could not be applied: {last_err}\n"
                    "Return ONLY a JSON array of {old, new} edits. Each 'old' must match the "
                    "current file EXACTLY and be unique (add surrounding context if needed)."
                )
                continue
        if abspath:
            abspath.parent.mkdir(parents=True, exist_ok=True)
            abspath.write_text(applied_content, encoding="utf-8")
        v = (
            await asyncio.to_thread(
                validate, spec, applied_content, abspath, work, allow_cmds
            )
            if spec
            else {"ok": True, "error": ""}
        )
        if v["ok"]:
            if share:
                board_set(
                    work,
                    oid,
                    {
                        "output_path": str(abspath) if abspath else None,
                        "result": applied_content[:2000],
                    },
                    agent=oid,
                )
            reg_update(
                work,
                oid,
                status="applied",
                attempts=attempts,
                output_path=str(abspath) if abspath else None,
            )
            event(work, "finish", oid, status="applied", attempts=attempts)
            run_hook(
                work,
                hooks,
                "on_finish",
                {"id": oid, "status": "applied", "output_path": out or ""},
                allow_cmds,
            )
            return {
                "id": oid,
                "status": "applied",
                "attempts": attempts,
                "output_path": str(abspath) if abspath else None,
                "usd": round(spent, 6),
                "preview": None if abspath else text[:500],
            }
        last_err = v["error"]
        cur_prompt = (
            f"{base_prompt}\n\nYour previous attempt FAILED validation:\n{last_err}\n"
            "Fix it and return ONLY the corrected output."
        )

    # final failure: roll the file back to its pre-run state
    if abspath:
        if original is not None:
            abspath.write_bytes(original)
        elif abspath.exists():
            abspath.unlink()
    reg_update(work, oid, status="failed", attempts=attempts, error=str(last_err)[:300])
    event(work, "fail", oid, error=str(last_err)[:200])
    run_hook(
        work,
        hooks,
        "on_fail",
        {
            "id": oid,
            "status": "failed",
            "output_path": out or "",
            "error": str(last_err)[:300],
        },
        allow_cmds,
    )
    return {
        "id": oid,
        "status": "failed",
        "attempts": attempts,
        "error": last_err,
        "output_path": str(abspath) if abspath else None,
        "usd": round(spent, 6),
    }


async def run_delegate(
    orders: list,
    work_dir: str,
    allow_commands: list = None,
    model: str = "",
    hooks: dict = None,
    reset: bool = False,
    fallback: str = "",
) -> dict:
    """Run a DAG of orders to completion. See server.delegate_run for the contract."""
    err = preflight(orders)
    if err:
        return {"error": err}
    if not workers.API_KEY:
        return {"error": "OPENROUTER_API_KEY is not set in this server's env."}
    allow_cmds = allow_commands or []
    work = str(pathlib.Path(work_dir).expanduser().resolve())
    if not os.path.isdir(work):
        return {"error": f"work_dir not found: {work}"}
    if reset:
        coord_clear(work)

    by_id = {o["id"]: o for o in orders}
    for o in orders:
        reg_update(
            work,
            o["id"],
            task=o.get("prompt", "")[:120],
            status="pending",
            depends_on=o.get("depends_on") or [],
        )

    results: dict = {}
    pending = set(by_id)
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
                if pending:  # nothing runnable and nothing skipped -> dependency cycle
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
            batch = await asyncio.gather(
                *[
                    process_order(
                        client,
                        {
                            **by_id[oid],
                            "model": by_id[oid].get("model") or model,
                            "fallback": by_id[oid].get("fallback") or fallback,
                        },
                        work,
                        allow_cmds,
                        hooks,
                    )
                    for oid in ready
                ]
            )
            for r in batch:
                results[r["id"]] = r
                pending.discard(r["id"])

    ordered = [results[o["id"]] for o in orders]
    applied = [r for r in ordered if r["status"] == "applied"]
    failed = [r for r in ordered if r["status"] == "failed"]
    skipped = [r for r in ordered if r["status"] == "skipped"]
    retried = [r for r in ordered if r.get("attempts", 0) > 1]
    total_usd = round(sum(r.get("usd", 0.0) for r in ordered), 6)
    return {
        "summary": {
            "total": len(ordered),
            "applied": len(applied),
            "failed": len(failed),
            "skipped": len(skipped),
            "retried": len(retried),
            "usd": total_usd,
            "failed_ids": [r["id"] for r in failed],
            "skipped_ids": [r["id"] for r in skipped],
        },
        "orders": ordered,
        "board": board_get(work),
        "registry": reg_get(work),
        "events": events_tail(work, 100),
    }
