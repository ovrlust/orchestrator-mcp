"""The cheap worker as a sandboxed, board-aware tool-calling agent."""

import os
import re
import json
import time
import shutil
import pathlib
import subprocess

import httpx

from workers import chat_resilient, SEM, DEFAULT_MODEL, MAX_FILE
from workers import context_budget
from compaction import maybe_compact
from ledger import record_spend
from coordination import board_get, board_set, reg_get, reg_update, event
from sandbox import safe_path, check_command
from toollog import log_call
import messages as msgbus
import presets

# A schema-gated agent gets a few extra steps so rejections can't starve the task.
MAX_SCHEMA_RETRIES = 2

# How many recent turns to keep verbatim when compacting (the budget itself is
# auto-derived from the worker model's real context window — see workers.context_budget).
KEEP_SEGMENTS = int(os.environ.get("DELEGATE_KEEP_SEGMENTS", "4"))
# Default line window for read_file when no explicit limit is given.
DEFAULT_READ_LINES = int(os.environ.get("DELEGATE_READ_LINES", "250"))
# Dirs the grep fallback skips (rg uses .gitignore instead).
IGNORE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".delegate",
    "dist",
    "build",
}


def load_rules(work: str) -> str:
    """Read AGENTS.md / CLAUDE.md project rules from work_dir (capped)."""
    parts = []
    for name in ("AGENTS.md", "CLAUDE.md"):
        try:
            txt = (pathlib.Path(work) / name).read_text(encoding="utf-8").strip()
            if txt:
                parts.append(f"# {name}\n{txt}")
        except Exception:  # noqa: BLE001
            pass
    return "\n\n".join(parts)[:8000]


from edits import apply_one, apply_edits, EditError

WORKER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a text file (relative to work_dir), returned with line numbers. "
                "Reads a window of lines, not the whole file — use offset (1-based start "
                "line) and limit to page through big files instead of dumping them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {
                        "type": "integer",
                        "description": "1-based start line (default 1)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"max lines (default {DEFAULT_READ_LINES})",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a NEW file, or fully replace one. For changing part of an existing file, prefer edit_file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Make a surgical edit: replace old_string with new_string in an existing file. "
                "You MUST read_file first. old_string must match EXACTLY and be unique (include "
                "surrounding context), unless replace_all is set. Preferred over write_file for edits."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "multi_edit",
            "description": (
                "Apply SEVERAL string replacements to one file atomically (in order). read_file first. "
                "Each edit's old_string must match exactly and be unique unless replace_all. If any edit "
                "fails to match, NOTHING is written."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_string": {"type": "string"},
                                "new_string": {"type": "string"},
                                "replace_all": {"type": "boolean"},
                            },
                            "required": ["old_string", "new_string"],
                        },
                    },
                },
                "required": ["path", "edits"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files by glob pattern (e.g. '**/*.py', 'src/*.ts') relative to work_dir.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "base dir (default '.')"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch a URL and return its readable text content (HTML stripped).",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web (requires TAVILY_API_KEY). Returns top results.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "download",
            "description": "Download a URL to a file inside work_dir.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}, "path": {"type": "string"}},
                "required": ["url", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_plan",
            "description": "Set/replace the task checklist (todos). Pass the full list each time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "plan": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "done": {"type": "boolean"},
                            },
                            "required": ["text"],
                        },
                    },
                },
                "required": ["plan"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List a directory (relative to work_dir; default '.').",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": (
                "Search files under work_dir (ripgrep, respects .gitignore). By default "
                "returns matching FILES with hit counts (cheap) — set content=true to see "
                "the actual matching lines for the file you care about. Use path to scope."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "content": {
                        "type": "boolean",
                        "description": "return matching lines, not just file:count",
                    },
                    "path": {
                        "type": "string",
                        "description": "subdir/file to scope the search (default whole repo)",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "cap output lines (default 50)",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_board",
            "description": "Read the shared blackboard (results other agents published). Optional key.",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_board",
            "description": "Publish a value to the shared blackboard for other agents to see.",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_agents",
            "description": "See the other agents: their tasks and statuses.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "post_message",
            "description": (
                "Send a message. Omit 'to' to broadcast to everyone (human + all agents); "
                "set 'to' to another agent_id to address it directly. Use to ask, report, or hand off."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "to": {
                        "type": "string",
                        "description": "recipient agent_id; empty = broadcast",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_messages",
            "description": "Pull messages sent to you (or broadcast) since you last checked. Check this to see replies or new directives.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run an ALLOWLISTED shell command in work_dir. Disabled unless permitted by the caller.",
            "parameters": {
                "type": "object",
                "properties": {"cmd": {"type": "string"}},
                "required": ["cmd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Signal the task is complete and provide a short summary.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        },
    },
]


def _grep(work, pattern, rel, content, maxr):
    """ripgrep search (gitignore-aware); names+counts by default, lines if content."""
    rg = shutil.which("rg")
    if not rg:
        return _grep_fallback(work, pattern, rel, content, maxr)
    flags = ["-n", "--no-heading"] if content else ["-c"]
    argv = [rg, "--no-messages", "-S", *flags, "--", pattern, rel]
    try:
        r = subprocess.run(argv, cwd=work, capture_output=True, text=True, timeout=30)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: rg failed: {e}"
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    if not lines:
        return "(no matches)"
    shown = [ln[:300] for ln in lines[:maxr]] if content else lines[:maxr]
    extra = f"\n... +{len(lines) - maxr} more" if len(lines) > maxr else ""
    note = (
        ""
        if content
        else "\n(files:count — call grep again with content=true on the file you want)"
    )
    return "\n".join(shown) + extra + note


def _grep_fallback(work, pattern, rel, content, maxr):
    """Pure-Python grep when ripgrep isn't installed; skips IGNORE_DIRS."""
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"ERROR: bad regex: {e}"
    base = pathlib.Path(work) / rel
    roots = [base] if base.is_file() else base.rglob("*")
    counts, out = {}, []
    for f in roots:
        if not f.is_file() or any(p in IGNORE_DIRS for p in f.parts):
            continue
        try:
            if f.stat().st_size > 1_000_000:
                continue
            for i, line in enumerate(
                f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
            ):
                if rx.search(line):
                    rp = f.relative_to(work)
                    if content:
                        out.append(f"{rp}:{i}:{line[:300]}")
                        if len(out) >= maxr:
                            break
                    else:
                        counts[str(rp)] = counts.get(str(rp), 0) + 1
        except Exception:  # noqa: BLE001
            pass
        if content and len(out) >= maxr:
            break
    if content:
        return "\n".join(out) or "(no matches)"
    if not counts:
        return "(no matches)"
    rows = [f"{p}:{c}" for p, c in sorted(counts.items(), key=lambda x: -x[1])][:maxr]
    return (
        "\n".join(rows)
        + "\n(files:count — call grep again with content=true on the file you want)"
    )


def _forget_windows(windows, t) -> None:
    """Drop read-window memory for a file the agent just modified, so a
    re-read of the new content isn't flagged as redundant."""
    if windows is not None:
        for w in [w for w in windows if w[0] == str(t)]:
            windows.discard(w)


def _schema_check(text: str, schema: dict):
    """Validate `text` as JSON against `schema`. Returns (obj, None) or (None, error).
    Tolerates the ```json fences cheap models love to add."""
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*\n?|\n?```\s*$", "", t)
    try:
        obj = json.loads(t)
    except Exception as e:  # noqa: BLE001
        return None, f"not valid JSON: {e}"
    try:
        import jsonschema

        jsonschema.validate(obj, schema)
    except ImportError:
        return None, "jsonschema not installed on the server"
    except Exception as e:  # noqa: BLE001 - validation error
        return None, f"schema mismatch: {getattr(e, 'message', e)}"
    return obj, None


def exec_tool(name, args, work, allow_cmds, changed, agent_id, seen=None, windows=None):
    seen = seen if seen is not None else set()
    try:
        if name == "read_file":
            t = safe_path(work, args["path"])
            seen.add(str(t))  # read-before-edit bookkeeping
            lines = t.read_text(encoding="utf-8", errors="replace").splitlines()
            total = len(lines)
            offset = max(1, int(args.get("offset", 1)))
            limit = int(args.get("limit", 0)) or DEFAULT_READ_LINES
            if windows is not None:
                key = (str(t), offset, limit)
                if key in windows:
                    return (
                        f"(you already read {args['path']} lines {offset}-{offset + limit - 1} — "
                        "that exact window is in your context above; use it, read a "
                        "DIFFERENT window, or move on)"
                    )
                windows.add(key)
            start = offset - 1
            chunk = lines[start : start + limit]
            end = start + len(chunk)
            numbered = "\n".join(
                f"{start + i + 1}\t{ln}" for i, ln in enumerate(chunk)
            )[:MAX_FILE]
            head = (
                f"[lines {offset}-{end} of {total}]\n" if (start or end < total) else ""
            )
            foot = (
                f"\n... [{total - end} more lines; read offset={end + 1} to continue]"
                if end < total
                else ""
            )
            return head + numbered + foot
        if name == "write_file":
            t = safe_path(work, args["path"])
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_text(args["content"], encoding="utf-8")
            changed.add(str(t))
            seen.add(str(t))  # the agent now knows this file's content
            _forget_windows(windows, t)
            return f"wrote {t} ({len(args['content'])} chars)"
        if name == "edit_file":
            t = safe_path(work, args["path"])
            if not t.exists():
                return f"ERROR: file does not exist: {args['path']} (use write_file to create it)"
            if str(t) not in seen:
                return f"ERROR: read_file {args['path']} before editing it"
            try:
                src = t.read_text(encoding="utf-8")
                out = apply_one(
                    src,
                    args["old_string"],
                    args["new_string"],
                    bool(args.get("replace_all", False)),
                )
            except EditError as e:
                return f"ERROR: {e}"
            t.write_text(out, encoding="utf-8")
            changed.add(str(t))
            _forget_windows(windows, t)
            delta = len(out) - len(src)
            return f"edited {t} ({'+' if delta >= 0 else ''}{delta} chars)"
        if name == "multi_edit":
            t = safe_path(work, args["path"])
            if not t.exists():
                return f"ERROR: file does not exist: {args['path']}"
            if str(t) not in seen:
                return f"ERROR: read_file {args['path']} before editing it"
            try:
                src = t.read_text(encoding="utf-8")
                out = apply_edits(src, args["edits"])
            except EditError as e:
                return f"ERROR: {e}"
            t.write_text(out, encoding="utf-8")
            changed.add(str(t))
            _forget_windows(windows, t)
            return f"multi-edited {t} ({len(args['edits'])} edits)"
        if name == "glob":
            root = safe_path(
                work, "."
            )  # resolved (handles symlinks like /var -> /private/var)
            base = safe_path(work, args.get("path", "."))
            matches = sorted(
                str(p.relative_to(root))
                for p in base.glob(args["pattern"])
                if p.is_file() and not any(part in IGNORE_DIRS for part in p.parts)
            )
            return "\n".join(matches[:200]) or "(no matches)"
        if name == "fetch_url":
            with httpx.Client(follow_redirects=True, timeout=60) as c:
                html = c.get(args["url"], headers={"user-agent": "delegate-mcp"}).text
            text = re.sub(
                r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I
            )
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            return text[:20000]
        if name == "web_search":
            key = os.environ.get("TAVILY_API_KEY", "")
            if not key:
                return "web_search unavailable (set TAVILY_API_KEY in the MCP env)"
            with httpx.Client(timeout=30) as c:
                data = c.post(
                    "https://api.tavily.com/search",
                    json={"api_key": key, "query": args["query"], "max_results": 5},
                ).json()
            return json.dumps(
                [
                    {
                        "title": r.get("title"),
                        "url": r.get("url"),
                        "content": r.get("content", "")[:500],
                    }
                    for r in data.get("results", [])
                ],
                indent=2,
            )
        if name == "download":
            dest = safe_path(work, args["path"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            with httpx.Client(follow_redirects=True, timeout=120) as c:
                content = c.get(args["url"]).content
            dest.write_bytes(content)
            changed.add(str(dest))
            return f"downloaded {len(content)} bytes -> {dest}"
        if name == "update_plan":
            board_set(work, "plan", args["plan"], agent=agent_id)
            done = sum(1 for p in args["plan"] if p.get("done"))
            return f"plan updated ({done}/{len(args['plan'])} done)"
        if name == "list_dir":
            t = safe_path(work, args.get("path", "."))
            return (
                "\n".join(
                    sorted(p.name + ("/" if p.is_dir() else "") for p in t.iterdir())
                )
                or "(empty)"
            )
        if name == "grep":
            pattern = args["pattern"]
            rel = args.get("path", ".")
            safe_path(work, rel)  # reject escapes; rg runs with cwd=work
            content = bool(args.get("content", False))
            maxr = int(args.get("max_results", 50))
            return _grep(work, pattern, rel, content, maxr)
        if name == "read_board":
            return json.dumps(board_get(work, args.get("key") or None), indent=2)
        if name == "write_board":
            board_set(work, args["key"], args["value"], agent=agent_id)
            return f"published board[{args['key']}]"
        if name == "list_agents":
            return json.dumps(reg_get(work), indent=2)
        if name == "post_message":
            to = args.get("to", "")
            n = msgbus.post_message(work, agent_id, args["text"], to)
            return f"posted message #{n}" + (f" to {to}" if to else " (broadcast)")
        if name == "read_messages":
            cur = reg_get(work).get(agent_id, {}).get("msg_cursor", 0)
            new = msgbus.read_messages(work, agent_id, since=cur)
            if new:
                reg_update(work, agent_id, msg_cursor=max(m["seq"] for m in new))
                return json.dumps(new, indent=2)
            return "(no new messages)"
        if name == "run_command":
            cmd = args["cmd"].strip()
            denied = check_command(cmd, allow_cmds)
            if denied:
                return f"DENIED ({denied})"
            r = subprocess.run(
                cmd, shell=True, cwd=work, capture_output=True, text=True, timeout=300
            )
            return f"exit={r.returncode}\n{(r.stdout + r.stderr)[-4000:]}"
        return f"unknown tool {name}"
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {type(e).__name__}: {e}"


async def run_agent_loop(
    task: str,
    work: str,
    model: str = "",
    agent_id: str = "agent",
    allow_cmds: list = None,
    max_steps: int = 25,
    system: str = "",
    agent_type: str = "general",
    output_schema: dict = None,
    messages: list = None,
    checkpoint=None,
    max_total_tokens: int = 0,
) -> dict:
    """Tool-calling agent loop inside work. See server.run_agent for the contract.

    agent_type picks a preset (system prompt + tool subset); output_schema forces
    done(summary) to be JSON matching the schema (rejections fed back, bounded
    retries); messages (a saved transcript, last entry a user message) resumes a
    previous run instead of starting fresh — task is ignored then. checkpoint, if
    given, is called as checkpoint(messages, step, changed) at the top of every
    step so the caller can persist progress for crash/cancel recovery.
    max_total_tokens (0 = unlimited) is a SOFT ceiling on prompt+completion
    tokens: it is checked between steps, so once crossed the next step is forced
    final (answer with what you have). Actual usage overshoots by the crossing
    step plus the final answer call — it's a runaway backstop for paid models,
    not an exact cap.
    """
    allow_cmds = allow_cmds or []
    if agent_type not in presets.PRESETS:
        return {
            "error": f"unknown agent_type '{agent_type}' (one of {sorted(presets.PRESETS)})"
        }
    allowed_tools = presets.tool_names(agent_type)
    tools = (
        WORKER_TOOLS
        if allowed_tools is None
        else [t for t in WORKER_TOOLS if t["function"]["name"] in allowed_tools]
    )
    reg_update(
        work,
        agent_id,
        task=(task or "(resumed)")[:120],
        status="running",
        kind=agent_type,
    )
    event(work, "start", agent_id, task=(task or "(resumed)")[:80])

    if messages is None:
        sys_prompt = system or presets.system_prompt(agent_type)
        sys_prompt += presets.REPORT_CONTRACT
        sys_prompt += (
            f"\n\nwork_dir = {work}\nyour agent_id = {agent_id}\n"
            f"allowed shell-command prefixes: {allow_cmds or 'NONE (run_command disabled)'}"
        )
        peers = {k: v for k, v in reg_get(work).items() if k != agent_id}
        if peers:
            roster = ", ".join(f"{k}({v.get('status', '?')})" for k, v in peers.items())
            sys_prompt += (
                f"\n\nOther agents in this work_dir right now: {roster}. "
                "Call list_agents / read_board to coordinate; publish anything they need with write_board."
            )
        # Project rules (AGENTS.md / CLAUDE.md from work_dir) — same as the harness.
        rules = load_rules(work)
        if rules:
            sys_prompt += f"\n\n## Project rules (follow these)\n{rules}"
        if output_schema:
            task += (
                "\n\nYour done(summary) MUST be ONLY a JSON object matching this schema:\n"
                + json.dumps(output_schema)
            )
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": task},
        ]
    changed = set()
    seen = set()  # files read this session (gates edit_file)
    windows = set()  # exact read windows already returned (redundant-read guard)
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    tail = []
    msg_cursor = 0  # last message seq this agent has been shown

    def _finish(status, result, steps):
        reg_update(work, agent_id, status=status, files=sorted(changed))
        event(work, "finish", agent_id, status=status)
        return {
            "result": result,
            "steps": steps,
            "files_changed": sorted(changed),
            "usage": usage,
            "transcript_tail": tail[-12:],
            "messages": messages,
        }

    schema_fails = 0
    total_steps = max_steps + (MAX_SCHEMA_RETRIES if output_schema else 0)
    async with httpx.AsyncClient() as client:
        for step in range(total_steps):
            messages, cinfo = await maybe_compact(
                client,
                messages,
                model or DEFAULT_MODEL,
                context_budget(model or DEFAULT_MODEL),
                KEEP_SEGMENTS,
            )
            if cinfo:
                usage["prompt_tokens"] += cinfo["usage"].get("prompt_tokens", 0)
                usage["completion_tokens"] += cinfo["usage"].get("completion_tokens", 0)
                record_spend(work, cinfo["model"], cinfo["usage"])
                event(work, "compact", agent_id, segments=cinfo["segments_compacted"])

            # Heartbeat: reflect live progress in the registry so the orchestrator
            # (and the monitor tool) can see this agent is alive and where it is.
            reg_update(work, agent_id, step=step + 1, last_active=round(time.time(), 3))

            # Checkpoint the transcript so a crash or cancel mid-step is resumable.
            if checkpoint:
                checkpoint(messages, step + 1, sorted(changed))

            # Push delivery: pull messages that arrived for this agent since we last
            # looked and inject them into the conversation, so a directive from the
            # orchestrator or another agent is acted on WITHOUT the agent having to
            # remember to poll. The agent's own posts are skipped (no echo).
            inbox = msgbus.read_messages(work, agent_id, since=msg_cursor)
            if inbox:
                msg_cursor = max(m.get("seq", msg_cursor) for m in inbox)
                # Keep the registry cursor in sync so a later read_messages tool
                # call doesn't re-deliver what push delivery already showed.
                reg_update(work, agent_id, msg_cursor=msg_cursor)
                inbound = [m for m in inbox if m.get("from") != agent_id]
                if inbound:
                    note = (
                        "📨 New messages (act on any directives, then continue):\n"
                        + "\n".join(
                            f"- from {m.get('from', '?')}"
                            + (" → you" if m.get("to") == agent_id else " (broadcast)")
                            + f": {m.get('text', '')}"
                            for m in inbound
                        )
                    )
                    messages.append({"role": "user", "content": note[:4000]})
                    tail.append(f"recv {len(inbound)} msg(s)")
                    event(work, "messages_delivered", agent_id, count=len(inbound))

            # Forced convergence: on the final normal step — or once the token
            # ceiling is crossed — drop every tool but `done` and tell the agent
            # so. A strong-reasoner/weak-loop-driver model (see
            # docs/deepseek-behavior.md) otherwise keeps gathering past the
            # budget and returns "(max_steps reached)" with no answer; this
            # converts that into a real best-effort answer.
            spent = usage["prompt_tokens"] + usage["completion_tokens"]
            budget_hit = bool(max_total_tokens) and spent >= max_total_tokens
            last_step = step == max_steps - 1 or budget_hit
            if last_step:
                step_tools = [
                    t for t in tools if t["function"]["name"] == "done"
                ] or tools
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "TOKEN BUDGET exhausted — FINAL STEP. "
                            if budget_hit
                            else "FINAL STEP — no more tool calls are available. "
                        )
                        + "Call done() now with your best answer from what you "
                        "have already gathered.",
                    }
                )
            else:
                step_tools = tools

            body = {
                "model": model or DEFAULT_MODEL,
                "messages": messages,
                "tools": step_tools,
                "tool_choice": "auto",
                "temperature": 0,
            }
            try:
                async with SEM:
                    data = await chat_resilient(client, body)
            except Exception as e:  # noqa: BLE001
                reg_update(work, agent_id, status="failed", error=str(e)[:200])
                event(work, "fail", agent_id, error=str(e)[:200])
                return {
                    "error": f"api: {type(e).__name__}: {e}",
                    "files_changed": sorted(changed),
                    "steps": step + 1,
                    "usage": usage,
                    "transcript_tail": tail[-12:],
                    "messages": messages,
                }
            u = data.get("usage", {})
            usage["prompt_tokens"] += u.get("prompt_tokens", 0)
            usage["completion_tokens"] += u.get("completion_tokens", 0)
            record_spend(work, body["model"], u)
            msg = data["choices"][0]["message"]
            # Re-send only the standard fields; provider extras (reasoning,
            # annotations, ...) can 400 on stricter OpenAI-compatible backends.
            clean = {"role": "assistant", "content": msg.get("content")}
            if msg.get("tool_calls"):
                clean["tool_calls"] = msg["tool_calls"]
            messages.append(clean)
            calls = msg.get("tool_calls")
            if not calls:
                content = msg.get("content", "")
                if output_schema:
                    obj, err = _schema_check(content, output_schema)
                    if err is None:
                        return _finish("done", obj, step + 1)
                    schema_fails += 1
                    if schema_fails > MAX_SCHEMA_RETRIES:
                        r = _finish("failed", content, step + 1)
                        r["error"] = f"output failed schema after {schema_fails} attempts: {err}"
                        return r
                    messages.append(
                        {
                            "role": "user",
                            "content": f"REJECTED ({err}). Call done(summary) where "
                            "summary is ONLY a JSON object matching the required schema.",
                        }
                    )
                    tail.append(f"schema reject: {err[:80]}")
                    continue
                return _finish("done", content, step + 1)
            for c in calls:
                fn = c["function"]["name"]
                try:
                    a = json.loads(c["function"].get("arguments") or "{}")
                except Exception:  # noqa: BLE001
                    a = {}
                if fn == "done":
                    summary = a.get("summary", "")
                    if output_schema:
                        obj, err = _schema_check(summary, output_schema)
                        if err is None:
                            return _finish("done", obj, step + 1)
                        schema_fails += 1
                        if schema_fails > MAX_SCHEMA_RETRIES:
                            r = _finish("failed", summary, step + 1)
                            r["error"] = f"output failed schema after {schema_fails} attempts: {err}"
                            return r
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": c.get("id", ""),
                                "content": f"REJECTED ({err}). Call done again; summary "
                                "must be ONLY a JSON object matching the required schema.",
                            }
                        )
                        tail.append(f"schema reject: {err[:80]}")
                        continue
                    return _finish("done", summary, step + 1)
                if allowed_tools is not None and fn not in allowed_tools:
                    res = f"ERROR: tool {fn} is not available to a '{agent_type}' agent"
                else:
                    res = exec_tool(
                        fn, a, work, allow_cmds, changed, agent_id, seen, windows
                    )
                log_call(
                    work,
                    agent_id,
                    step + 1,
                    fn,
                    a,
                    res,
                    ok=not str(res).startswith("ERROR"),
                )
                tail.append(f"{fn} {str(a)[:60]} -> {str(res)[:120]}")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": c.get("id", ""),
                        "content": str(res)[:6000],
                    }
                )
            # Last step and the agent kept tool-calling instead of finishing
            # (some models ignore a done-only toolset). Don't burn the run for
            # nothing — make ONE final no-tools call to extract its answer.
            if last_step:
                messages.append(
                    {
                        "role": "user",
                        "content": "STOP. You are out of tool calls. Reply with your "
                        "final answer to the original task as plain text"
                        + (
                            " — ONLY a JSON object matching the required schema."
                            if output_schema
                            else "."
                        ),
                    }
                )
                try:
                    async with SEM:
                        final = await chat_resilient(
                            client,
                            {
                                "model": model or DEFAULT_MODEL,
                                "messages": messages,
                                "temperature": 0,
                            },
                        )
                    record_spend(work, model or DEFAULT_MODEL, final.get("usage", {}))
                    text = final["choices"][0]["message"].get("content", "") or ""
                except Exception:  # noqa: BLE001 - fall through to incomplete
                    text = ""
                if output_schema:
                    obj, err = _schema_check(text, output_schema)
                    if err is None:
                        return _finish("done", obj, total_steps)
                    r = _finish("incomplete", text or "(no answer)", total_steps)
                    r["error"] = f"ran out of steps; final answer failed schema: {err}"
                    return r
                if text.strip():
                    return _finish("incomplete", text, total_steps)
        reg_update(work, agent_id, status="incomplete", files=sorted(changed))
        event(work, "finish", agent_id, status="incomplete")
        return {
            "result": "(max_steps reached without done)",
            "steps": total_steps,
            "files_changed": sorted(changed),
            "usage": usage,
            "transcript_tail": tail[-12:],
            "messages": messages,
        }
