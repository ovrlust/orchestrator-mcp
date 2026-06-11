"""The orchestrator's toolset: the worker file/board/message tools plus the two
that make it an orchestrator — `delegate` (a DAG of cheap workers) and
`spawn_agent` (one autonomous cheap worker). Everything dispatches to the
existing modules; the orchestrator operates in the session's cwd.
"""

import sys
import json
import asyncio
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import agent  # noqa: E402
import delegate as delegate_mod  # noqa: E402

# Worker tools the orchestrator also gets (everything except the worker-only `done`).
_WORKER = [t for t in agent.WORKER_TOOLS if t["function"]["name"] != "done"]
_WORKER_NAMES = {t["function"]["name"] for t in _WORKER}

_EXTRA = [
    {
        "type": "function",
        "function": {
            "name": "delegate",
            "description": (
                "Run a DAG of fully-specified work orders on cheap workers (plan -> worker -> "
                "apply -> validate -> retry). Decompose the task and write a validator per order; "
                "the workers execute the grind. Returns a report."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "orders": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "each: {id, prompt, output_path?, edit?, validate?, depends_on?, share?, model?}",
                    },
                    "allow_commands": {"type": "array", "items": {"type": "string"}},
                    "model": {"type": "string"},
                    "fallback": {"type": "string"},
                },
                "required": ["orders"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_agent",
            "description": (
                "Spawn ONE autonomous cheap worker as a tool-calling agent in the cwd. Use for a "
                "self-contained sub-task that needs exploration/iteration. Returns its result + files changed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "agent_id": {"type": "string"},
                    "model": {"type": "string"},
                    "allow_commands": {"type": "array", "items": {"type": "string"}},
                    "max_steps": {"type": "integer"},
                },
                "required": ["task"],
            },
        },
    },
]

ORCH_TOOLS = _WORKER + _EXTRA


def toolset(mode: str) -> list:
    """Tools for a session. 'solo' = a normal single-agent harness (no worker
    dispatch); 'delegate' = also gets `delegate` + `spawn_agent`."""
    return list(_WORKER) if mode == "solo" else ORCH_TOOLS


def _narrow_allow(requested, server_allow: list) -> list:
    """The model may narrow the session allowlist for its workers, never widen
    it — otherwise `allow_commands` in a tool call grants arbitrary shell."""
    if not requested:
        return server_allow
    return [c for c in requested if c in server_allow]


async def dispatch(name: str, args: dict, ctx: dict) -> str:
    """Run one orchestrator tool call. ctx: {work, allow_cmds, seen, changed, model}."""
    work = ctx["work"]
    allow = ctx.get("allow_cmds", [])
    if name in _WORKER_NAMES:
        return await asyncio.to_thread(
            agent.exec_tool,
            name,
            args,
            work,
            allow,
            ctx["changed"],
            "orchestrator",
            ctx["seen"],
        )
    if name == "delegate":
        res = await delegate_mod.run_delegate(
            args.get("orders", []),
            work,
            _narrow_allow(args.get("allow_commands"), allow),
            args.get("model") or ctx.get("model", ""),
            None,
            False,
            args.get("fallback", ""),
        )
        return json.dumps(res)[:8000]
    if name == "spawn_agent":
        res = await agent.run_agent_loop(
            args.get("task", ""),
            work,
            args.get("model") or ctx.get("model", ""),
            args.get("agent_id") or "agent",
            _narrow_allow(args.get("allow_commands"), allow),
            int(args.get("max_steps", 25)),
            "",
        )
        return json.dumps(res)[:8000]
    return f"unknown tool: {name}"
