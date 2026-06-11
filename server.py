#!/usr/bin/env python3
"""delegate - MCP for the orchestrator->worker pattern, with multi-agent coordination.

Claude (smart, expensive) PLANS and VALIDATES. Cheap external models (the
"workers", via OpenRouter) EXECUTE. This file is the thin MCP surface: each tool
is a wrapper that resolves work_dir and delegates to a focused module.

Modules:
  store         lock + .delegate/ path helpers
  ledger        model catalog, pricing, spend ledger
  sandbox       path confinement + command denylist
  coordination  blackboard, registry, events, hooks, DAG scheduler
  validators    deterministic output gates
  workers       OpenRouter client + config
  delegate      the autonomous DAG loop
  agent         the sandboxed tool-calling worker

Env:
  OPENROUTER_API_KEY   required for any live call
  ASK_MODEL_DEFAULT    optional default model (default openai/gpt-4o-mini)
"""

import pathlib

try:  # README's documented setup is `cp .env.example .env` — honor it.
    from dotenv import load_dotenv

    load_dotenv(pathlib.Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

import os

import httpx
from mcp.server.fastmcp import FastMCP

import workers
import ledger
import coordination as coord
import messages as msgbus
import project
import cache
import toollog
from workers import call_model
from delegate import run_delegate
from agent import run_agent_loop
from director import run_director

mcp = FastMCP("delegate")


def _resolve(work_dir: str) -> str:
    return str(pathlib.Path(work_dir).expanduser().resolve())


def _existing(work_dir: str) -> str | None:
    """Resolve work_dir only if it exists — a typo'd path must error, not
    silently grow a .delegate/ state dir somewhere random."""
    work = _resolve(work_dir)
    return work if os.path.isdir(work) else None


# ------------------------- text workers -------------------------


def _maybe_record(work_dir: str, r: dict) -> None:
    """Log a call's spend to the work_dir ledger when one was given."""
    if work_dir and isinstance(r, dict) and "error" not in r:
        work = _existing(work_dir)
        if work:
            ledger.record_spend(work, r.get("model", "?"), r.get("usage", {}))


@mcp.tool()
async def ask_model(
    prompt: str,
    model: str = "",
    system: str = "",
    temperature: float = 0.0,
    max_tokens: int = 0,
    work_dir: str = "",
) -> dict:
    """Run ONE fully-specified, stateless work order (no tools). Returns {text, model, usage} or {error}.
    Pass work_dir to log the call's cost to that project's spend ledger."""
    async with httpx.AsyncClient() as client:
        r = await call_model(client, prompt, model, system, temperature, max_tokens)
    _maybe_record(work_dir, r)
    return r


@mcp.tool()
async def ask_model_batch(orders: list[dict], work_dir: str = "") -> list:
    """Run MANY independent stateless orders concurrently. Each: {prompt, model?, system?, temperature?, max_tokens?}.
    Pass work_dir to log each call's cost to that project's spend ledger."""
    import asyncio

    if not isinstance(orders, list) or not orders:
        return [{"error": "orders must be a non-empty list"}]
    bad = [i for i, o in enumerate(orders) if not isinstance(o, dict)]
    if bad:
        return [{"error": f"orders at index {bad} are not objects"}]

    async with httpx.AsyncClient() as client:
        tasks = [
            call_model(
                client,
                o.get("prompt", ""),
                o.get("model", ""),
                o.get("system", ""),
                float(o.get("temperature", 0.0) or 0.0),
                int(o.get("max_tokens", 0) or 0),
            )
            for o in orders
        ]
        results = await asyncio.gather(*tasks)
    for r in results:
        _maybe_record(work_dir, r)
    return results


@mcp.tool()
def understand_project(path: str) -> dict:
    """Scan a project ONCE into a cached structural map (file tree + symbols per file),
    keyed by content hash. Re-run anytime: it's incremental — only files whose content
    changed are re-read; unchanged files are reused. Cheap + deterministic (no model
    calls). Returns {total_files, added, changed, removed, reused}."""
    return project.understand(path)


@mcp.tool()
def project_context(path: str, max_files: int = 400) -> dict:
    """Return the cached project map (read THIS instead of re-reading the repo). Compact:
    each file's path, line count, and top-level symbols, ranked by symbol richness. Call
    understand_project first if not cached. Lets Claude grok a project without re-reading it."""
    return project.context(path, max_files)


@mcp.tool()
def project_overview(path: str) -> dict:
    """High-signal architecture digest of a repo (FREE, zero-LLM): entrypoints, core
    modules ranked by how many files import them (where the logic lives), language mix,
    and each core file's role. Read THIS first to know WHERE things are, then use
    clean/grep to dive. Call understand_project first if not cached."""
    return project.overview(path)


@mcp.tool()
async def summarize_project(path: str, model: str = "", limit: int = 0) -> dict:
    """OPT-IN cheap-LLM layer over the map: give each code file a 1-line role summary
    (incremental — only un-summarized/changed files cost anything; keyed by content
    hash). project_overview then shows these instead of raw symbol names. `limit` caps
    files summarized this call. Costs worker tokens; the structural map stays free."""
    return await project.summarize_project(path, model, limit)


@mcp.tool()
def list_models() -> str:
    """List curated cheap worker models with rough per-1M-token prices."""
    lines = ["Cheap workers (USD per 1M tokens, in / out):", ""]
    for mid, price, note, _i, _o in ledger.MODELS:
        lines.append(f"  {mid:<40} {price:<16} {note}")
    lines.append("")
    lines.append(f"Default: {workers.DEFAULT_MODEL}")
    return "\n".join(lines)


@mcp.tool()
def get_spend(work_dir: str) -> dict:
    """Total worker spend logged for a work_dir (tokens + USD, broken down by model)."""
    work = _existing(work_dir)
    if not work:
        return {"error": f"work_dir not found: {work_dir}"}
    return ledger.spend_summary(work)


@mcp.tool()
def cache_stats(clear: bool = False) -> dict:
    """Inspect the worker result cache (deterministic ask_model/order responses are
    cached on disk so identical re-runs cost $0 and return instantly). Returns
    {enabled, dir, entries, bytes}. Pass clear=true to wipe it (returns {removed})."""
    if clear:
        return cache.clear()
    return cache.stats()


# ------------------------- coordination -------------------------


@mcp.tool()
def board_read(work_dir: str, key: str = "") -> dict:
    """Read the shared blackboard for a work_dir (whole board, or one key)."""
    work = _resolve(work_dir)
    if key:
        return {"key": key, "value": coord.board_get(work, key)}
    return {"board": coord.board_get(work)}


@mcp.tool()
def board_write(work_dir: str, key: str, value: str) -> dict:
    """Publish a value to the shared blackboard (seed context before a run, or record a decision)."""
    work = _existing(work_dir)
    if not work:
        return {"error": f"work_dir not found: {work_dir}"}
    coord.board_set(work, key, value, agent="orchestrator")
    return {"ok": True, "key": key}


@mcp.tool()
def agents(work_dir: str) -> dict:
    """The live agent roster: who exists, their task, status, and files touched."""
    return coord.reg_get(_resolve(work_dir))


@mcp.tool()
def monitor(work_dir: str, events_limit: int = 30, messages_limit: int = 20) -> dict:
    """ONE live view of everything happening in a work_dir — call this to watch a run.

    Returns the agent roster (status, current step, last_active timestamp, files
    touched), the recent lifecycle events (start/finish/fail/message/compact/…),
    the keys currently on the shared board, and the latest messages on the bus.
    Because agents heartbeat into the registry and append to these logs live, the
    orchestrator can read this WHILE agents run (e.g. from a second session) to see
    progress, spot a stalled agent, or follow the agent-to-agent conversation."""
    work = _resolve(work_dir)
    board = coord.board_get(work) or {}
    return {
        "agents": coord.reg_get(work),
        "events": coord.events_tail(work, events_limit),
        "board_keys": sorted(board.keys()),
        "messages": msgbus.read_messages(work, "", 0)[-messages_limit:],
    }


@mcp.tool()
def events(work_dir: str, limit: int = 50) -> list:
    """Tail the lifecycle event log (start/finish/fail/hook/board_set)."""
    return coord.events_tail(_resolve(work_dir), limit)


@mcp.tool()
def tool_log(
    work_dir: str,
    limit: int = 100,
    agent: str = "",
    fn: str = "",
    errors_only: bool = False,
) -> list:
    """The durable per-tool-call log for run_agent workers: every read_file/grep/
    edit_file/run_command/etc. an agent invoked, with truncated args, a result
    preview, ok/err, step, and agent_id. Use it to audit what a cheap worker did,
    debug a bad run after it returns, or review every shell command executed.
    Filter by `agent` (agent_id) or `fn` (tool name); errors_only keeps only failed
    calls. Returns up to `limit` most-recent records."""
    return toollog.tail(_resolve(work_dir), limit, agent, fn, errors_only)


@mcp.tool()
def coord_reset(work_dir: str) -> dict:
    """Wipe board, registry, events, and messages for a fresh coordinated run (ledger kept)."""
    work = _existing(work_dir)
    if not work:
        return {"error": f"work_dir not found: {work_dir}"}
    coord.coord_clear(work)
    return {"ok": True}


@mcp.tool()
def send_message(
    work_dir: str, text: str, to: str = "", frm: str = "orchestrator"
) -> dict:
    """Send a message to an agent (set `to` to its agent_id) or broadcast (to='').

    Use this to steer a running agent, answer its question, or hand it new context.
    Agents pick it up via their read_messages tool.
    """
    work = _existing(work_dir)
    if not work:
        return {"error": f"work_dir not found: {work_dir}"}
    seq = msgbus.post_message(work, frm, text, to)
    return {"ok": True, "seq": seq}


@mcp.tool()
def read_messages(work_dir: str, agent: str = "", since: int = 0) -> list:
    """Read the message bus. agent='' sees everything; a named agent sees broadcasts
    + messages addressed to/from it. `since` returns only messages after that seq."""
    return msgbus.read_messages(_resolve(work_dir), agent, since)


# ------------------------- delegate loop -------------------------


@mcp.tool()
async def delegate_run(
    orders: list[dict],
    work_dir: str,
    allow_commands: list = None,
    model: str = "",
    hooks: dict = None,
    reset: bool = False,
    fallback: str = "",
) -> dict:
    """Autonomously run a DAG of fully-specified work orders to completion.

    YOU (Claude) decompose a spec into orders; this loop executes each one
    worker -> apply -> validate -> retry-once -> report, respecting dependencies,
    and rolls back any order that can't pass its validator. No Claude judgment is
    used mid-run; everything must be pre-specified.

    Each order:
      id            label, used across the report/registry/board
      prompt        the fully-specified work order
      model?        override worker model (else `model` arg, else default)
      system?       optional system prompt
      output_path?  file (relative to work_dir) to write the result to; backed up
                    first and restored if the order ultimately fails
      edit?         if true, treat the worker's output as a JSON array of
                    {old, new} edits applied surgically to output_path (which must
                    exist) instead of overwriting it. Cheaper + safer for changing
                    part of a file; bad/ambiguous edits are fed back and retried.
      validate?     gate: {type: nonempty|regex|json|shell, ...}
      depends_on?   [id,...] - run only after these finish; their published output
                    + a board snapshot are injected into this order's prompt. If a
                    dependency failed, this order is skipped.
      share?        if true, publish this order's result to the shared board under
                    its id so dependents can read it
      max_retries?  retries after first failure (default 1)

    allow_commands  shell-prefix allowlist for shell validators AND shell hooks
    hooks           {on_start?, on_finish?, on_fail?} shell templates run at each
                    lifecycle point; {id}/{status}/{output_path}/{error} substituted
    reset           wipe board/registry/events before running (ledger kept)
    fallback        model to retry an order on once its primary model keeps
                    failing (per-order `fallback` overrides this)

    Order failures are also auto-retried with exponential backoff on transient
    upstream errors (429/5xx/timeout); concurrent worker calls are capped
    (DELEGATE_MAX_CONCURRENCY, default 8).

    Returns {summary, orders, board, registry, events}.
    """
    return await run_delegate(
        orders, work_dir, allow_commands, model, hooks, reset, fallback
    )


# ------------------------- agent worker -------------------------


@mcp.tool()
async def run_agent(
    task: str,
    work_dir: str,
    model: str = "",
    agent_id: str = "agent",
    allow_commands: list = None,
    max_steps: int = 25,
    system: str = "",
) -> dict:
    """Run the cheap worker as a TOOL-CALLING agent inside work_dir.

    Worker tools: read_file (windowed), write_file, edit_file, list_dir, grep
    (ripgrep, names-first), read_board, write_board, list_agents, post_message,
    read_messages, run_command, done. edit_file makes surgical str-replace edits
    (read-before-edit enforced, match must be unique). The agent registers itself,
    fires lifecycle events, shares the board/registry, and can message other
    agents or the orchestrator over the bus.

    Rails: paths confined to work_dir; run_command DISABLED unless `allow_commands`
    prefixes are passed (e.g. ["pytest","npm test"]); a hard denylist blocks
    dangerous patterns regardless.
    Returns {result, steps, files_changed, usage, transcript_tail} or {error}.
    """
    if not workers.API_KEY:
        return {"error": "OPENROUTER_API_KEY is not set in this server's env."}
    work = _resolve(work_dir)
    if not os.path.isdir(work):
        return {"error": f"work_dir not found: {work}"}
    return await run_agent_loop(
        task, work, model, agent_id, allow_commands, max_steps, system
    )


# ------------------------- director / supervisor -------------------------


@mcp.tool()
async def direct(
    sections: list,
    work_dir: str,
    model: str = "",
    allow_commands: list = None,
    reset: bool = False,
) -> dict:
    """DIRECTOR (fire-and-forget): split a plan into sections, each run by its OWN
    autonomous agent IN PARALLEL, respecting depends_on. The orchestrator (you) is
    NOT in the loop while they run — cheapest mode, best token economics. Because
    multiple agents are live at once they coordinate via the shared board + message
    bus (messages are pushed to them automatically); a dependency's published result
    is injected into dependents' tasks.

    Each section: {id, task, model?, depends_on?, allow_commands?, max_steps?,
    system?, validate?}. validate is an optional deterministic gate run AFTER the
    agent finishes ({type: shell|json|regex|nonempty,...}); failing it marks the
    section failed (no rollback — agents touch many files).

    Returns {summary, sections, monitor}. Use this for execution-heavy, parallel work
    where sub-tasks may need to coordinate live (e.g. one builds the API, another the
    client, they sync on the contract over the board)."""
    return await run_director(
        sections, work_dir, model, allow_commands, reset, supervise=False
    )


@mcp.tool()
async def supervise(
    sections: list,
    work_dir: str,
    model: str = "",
    supervisor_model: str = "",
    allow_commands: list = None,
    reset: bool = False,
    max_polls: int = 20,
    poll_interval: float = 2.0,
) -> dict:
    """SUPERVISOR (live watch): same parallel-agent dispatch as `direct`, but while a
    wave runs a supervisor MODEL polls the live state every poll_interval seconds and
    may message a struggling agent or stop the run. More reliable, MORE EXPENSIVE
    (each poll is a model call) — use when correctness matters more than cost.

    supervisor_model picks the watcher (default: the worker `model`); use a stronger
    model here for better judgment. max_polls caps the number of supervision rounds
    per wave. Returns {summary, sections, monitor, supervision} where supervision is
    the log of each poll's decision."""
    return await run_director(
        sections,
        work_dir,
        model,
        allow_commands,
        reset,
        supervise=True,
        supervisor_model=supervisor_model,
        max_polls=max_polls,
        poll_interval=poll_interval,
    )


if __name__ == "__main__":
    mcp.run()
