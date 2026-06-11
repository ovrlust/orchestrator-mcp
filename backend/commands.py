"""Slash commands — a harness feature (the LLM API gives you none of these).

A message that starts with '/' is routed here instead of to the model. Commands
operate on the session and emit a `command` event so the UI can render the
result. /compact is the headline: it summarizes older turns and rewrites the
transcript (the same idea as the worker-loop auto-compaction, at session level).
"""

import sys
import json
import asyncio
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import ledger  # noqa: E402

from . import session as sessions, providers, orchestrator


# ------------------------- pure helpers -------------------------


def compact_split(messages: list, keep_tail: int):
    """Split into (old, recent) keeping the last `keep_tail` messages, but never
    starting `recent` on an orphaned tool result (whose assistant call is in old)."""
    if len(messages) <= keep_tail:
        return [], messages
    start = len(messages) - keep_tail
    while start > 0 and messages[start].get("role") == "tool":
        start -= 1
    return messages[:start], messages[start:]


def _render(msgs: list) -> str:
    out = []
    for m in msgs:
        r = m.get("role", "?")
        for tc in m.get("tool_calls") or []:
            out.append(
                f"[{r} call {tc.get('name')}] {json.dumps(tc.get('args', {}))[:200]}"
            )
        if m.get("content"):
            out.append(f"[{r}] {str(m['content'])[:500]}")
    return "\n".join(out)


async def _summarize(session, old: list) -> str:
    msgs = [
        {
            "role": "system",
            "content": "Summarize this coding session so it can continue without the "
            "full log: what was attempted, key decisions, files changed, and what remains. Be terse.",
        },
        {"role": "user", "content": _render(old)},
    ]
    parts = []
    async for ev in providers.stream(
        session.provider, msgs, None, session.model, max_tokens=600
    ):
        if ev["type"] == "text":
            parts.append(ev["text"])
        elif ev["type"] == "error":
            return f"(summary failed: {ev['error']})"
    return "".join(parts).strip() or "(no summary)"


# ------------------------- command handlers -------------------------


async def cmd_compact(session, args):
    if orchestrator.is_running(session.id):
        return {
            "ok": False,
            "message": "can't compact while a turn is running — /interrupt first",
        }
    keep = int(args[0]) if args and args[0].isdigit() else 6
    old, recent = compact_split(session.messages, keep)
    if not old:
        return {"ok": True, "message": "nothing to compact"}
    summary = await _summarize(session, old)
    session.messages = [
        {
            "role": "user",
            "content": "[CONTEXT COMPACTED — summary of earlier turns]\n" + summary,
        }
    ] + recent
    sessions.save(session)
    return {
        "ok": True,
        "message": f"compacted {len(old)} messages into a summary; {len(recent)} kept",
    }


def cmd_clear(session, args):
    if orchestrator.is_running(session.id):
        return {
            "ok": False,
            "message": "can't clear while a turn is running — /interrupt first",
        }
    n = len(session.messages)
    session.messages = []
    sessions.save(session)
    return {"ok": True, "message": f"cleared {n} messages"}


def cmd_model(session, args):
    if not args:
        return {
            "ok": True,
            "message": f"provider={session.provider} model={session.model or '(default)'}",
        }
    if len(args) >= 2 and args[0] in providers.PROVIDERS:
        session.provider, session.model = args[0], args[1]
    elif args[0] in providers.PROVIDERS:
        session.provider = args[0]
    else:
        session.model = args[0]
    sessions.save(session)
    return {
        "ok": True,
        "message": f"provider={session.provider} model={session.model or '(default)'}",
    }


def cmd_mode(session, args):
    if not args:
        return {
            "ok": True,
            "message": f"mode={session.mode} (solo=single agent, delegate=+workers)",
        }
    m = args[0]
    if m not in ("solo", "delegate"):
        return {"ok": False, "message": "usage: /mode solo|delegate"}
    session.mode = m
    sessions.save(session)
    return {"ok": True, "message": f"mode={m}"}


def cmd_cost(session, args):
    s = ledger.spend_summary(session.cwd)
    return {
        "ok": True,
        "message": f"${s.get('usd', 0):.4f} over {s.get('calls', 0)} worker calls",
        "data": s,
    }


def cmd_title(session, args):
    if not args:
        return {"ok": True, "message": f"title={session.title}"}
    session.title = " ".join(args)
    sessions.save(session)
    return {"ok": True, "message": f"title={session.title}"}


def cmd_help(session, args):
    lines = [f"/{n}  —  {desc}" for n, (_h, desc) in sorted(COMMANDS.items())]
    return {"ok": True, "message": "\n".join(lines)}


COMMANDS = {
    "compact": (
        cmd_compact,
        "summarize older turns and shrink the transcript [keep_tail]",
    ),
    "clear": (cmd_clear, "wipe the conversation transcript"),
    "model": (cmd_model, "show or set provider/model: /model [provider] <model>"),
    "mode": (cmd_mode, "show or set mode: /mode solo|delegate"),
    "cost": (cmd_cost, "show worker spend for this session"),
    "title": (cmd_title, "show or rename the session"),
    "help": (cmd_help, "list commands"),
}


def is_command(text: str) -> bool:
    return text.strip().startswith("/")


def catalog() -> list:
    return [{"name": n, "help": d} for n, (_h, d) in sorted(COMMANDS.items())]


async def run(session, text: str) -> dict:
    parts = text.strip().split()
    name = parts[0][1:] if parts[0].startswith("/") else parts[0]
    args = parts[1:]
    entry = COMMANDS.get(name)
    if not entry:
        res = {"ok": False, "message": f"unknown command /{name} — try /help"}
    else:
        out = entry[0](session, args)
        res = await out if asyncio.iscoroutine(out) else out
    sessions.append_event(
        session.id,
        {
            "type": "command",
            "name": name,
            "ok": res.get("ok", True),
            "result": res.get("message", ""),
        },
    )
    return res
