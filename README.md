# delegate

An MCP server for the **orchestrator → worker** pattern.

Claude (smart, expensive) **plans and validates**. A cheap external model (the
*worker*, via [OpenRouter](https://openrouter.ai)) **executes**. The point is to
push high-volume, fully-specifiable grind onto the cheap model while Claude keeps
the judgment — and to do it without Claude babysitting every unit.

## Two ways to use it

1. **Standalone harness** (browser frontend → HTTP backend). The backend has its
   own LLM **orchestrator** (a strong, configurable model) that plans, edits,
   searches, and delegates to cheap workers — streaming everything to the browser
   over SSE. This is the "full harness like opencode" mode.
2. **MCP server** (`server.py`) — the same capabilities as tools for an external
   Claude to drive. Bonus interface; unchanged.

### Harness backend

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m backend          # serves http://127.0.0.1:8787
```

REST + SSE; the full API the frontend targets is in **[CONTRACT.md](CONTRACT.md)**.
In short:

- `POST /api/sessions {cwd, provider?, model?}` — start a conversation bound to a
  dir, with a per-session orchestrator brain (`openrouter` or `anthropic`).
- `POST /api/sessions/{id}/message {text}` — a user turn; the orchestrator streams
  tokens / tool-calls / results / worker+board+chat activity over
  `GET /api/sessions/{id}/stream` (SSE, resumable via `Last-Event-ID`).
- `POST .../interrupt`, `POST .../approve`, `POST .../messages` (human↔agent), and
  REST snapshots for `/board` `/agents` `/messages` `/spend`.
- **Slash commands** (`/compact`, `/clear`, `/model`, `/mode`, `/cost`, `/help`,
  `/title`) — harness-side, not the LLM. A `/`-prefixed message is run as a
  command; also `POST /command` and `GET /api/commands`. `/mode solo` turns off
  worker dispatch (a plain single-agent harness); `/mode delegate` turns it back on.

Backend layout: `backend/providers.py` (normalized streaming over OpenRouter +
Anthropic), `session.py` (persistence + event pub/sub), `tools.py` (orchestrator
toolset = worker tools + `delegate` + `spawn_agent`, filtered by mode),
`commands.py` (slash commands), `orchestrator.py` (the streaming tool-loop brain),
`app.py` (FastAPI REST + SSE).

## Tools

| Tool | What it does |
|------|--------------|
| `ask_model` | One stateless work order to a worker. `{text, model, usage}` |
| `ask_model_batch` | Many independent orders, concurrently |
| `delegate_run` | **The autonomous loop.** A DAG of orders → worker → apply → validate → retry-once → rollback-on-fail → share → report |
| `run_agent` | A worker as a sandboxed, board-aware tool-calling agent (incl. surgical `edit_file`) inside `work_dir` |
| `board_read` / `board_write` | Read/seed the shared blackboard |
| `send_message` / `read_messages` | Talk to agents (directed or broadcast) over the message bus |
| `agents` | The live agent roster (who exists, task, status, files) |
| `events` | Tail the lifecycle event log |
| `coord_reset` | Wipe board/registry/events/messages for a fresh run (ledger kept) |
| `list_models` | Curated cheap workers with prices |
| `get_spend` | Total worker spend logged for a `work_dir` (tokens + USD) |

## The delegate loop

You decompose a spec into fully-specified orders, each with a deterministic
validator, and hand the batch to `delegate_run`. It runs every order to
completion on its own and returns a report. No model judgment is used mid-run —
everything is pre-specified by you.

```jsonc
{
  "orders": [
    {
      "id": "translate-fr-01",
      "prompt": "Translate to French. Return ONLY the translation.\n\nHello world",
      "output_path": "out/fr_01.txt",
      "validate": { "type": "nonempty", "min_len": 3 },
      "max_retries": 1
    },
    {
      "id": "fix-types",
      "prompt": "<full file + instruction inlined>",
      "model": "deepseek/deepseek-chat",
      "output_path": "src/foo.py",
      "validate": { "type": "shell", "cmd": "python -c \"import ast,sys; ast.parse(open('src/foo.py').read())\"" }
    }
  ],
  "work_dir": "/path/to/project",
  "allow_commands": ["python -c", "pytest"]
}
```

Per order: dispatch → write result to `output_path` (backing up any existing
file) → run the validator → on failure, retry once with the error fed back → if
it still fails, **restore the original file** and mark the order `failed`. Never
loops forever.

### Surgical edits

Overwriting a whole file to change a few lines is how cheap-model harnesses go
wrong — drift, plus wasted output tokens. Two ways to edit in place instead:

- **`run_agent`** has an `edit_file(path, old_string, new_string, replace_all?)`
  tool. Read-before-edit is enforced and `old_string` must be unique (or
  `replace_all`); bad matches return a precise error the agent can fix.
- **`delegate_run`** orders can set `"edit": true`. The worker then returns a
  JSON array of `{old, new}` edits (not a whole file); they're applied to the
  existing `output_path`, validated, and on a bad/ambiguous edit the exact error
  is fed back for a retry. The shared `edits.py` core does the matching.

### Validators (`validate`)

Deterministic gates, no model judgment:

- `nonempty` — `{min_len?, max_len?}`; also rejects refusal phrases
- `regex` — `{pattern, must_not?}`
- `json` — `{schema?}` (schema needs `jsonschema`)
- `shell` — `{cmd}`; exit 0 = pass. Runs in `work_dir`, requires the prefix in
  `allow_commands`, and is subject to the dangerous-pattern denylist regardless.

Omit `validate` to apply without a gate (Claude validates later).

## Multi-agent coordination

Agents are aware of each other through shared state under `work_dir/.delegate/`:

| File | Role |
|------|------|
| `board.json` | **Blackboard** — agents publish results others can read |
| `registry.json` | **Roster** — who exists, their task, status, files touched |
| `events.jsonl` | **Lifecycle log** — start / finish / fail / hook / board_set |
| `ledger.json` | Per-call token + USD cost |

All writes are lock-guarded (single process, asyncio + worker threads).

**Dependencies (DAG).** An order can declare `depends_on: [id, ...]`. It runs
only after those finish, and its prompt is auto-prefixed with a board snapshot +
its dependencies' published output — that's how a downstream agent "sees" what an
upstream one produced. If a dependency fails, the dependent is `skipped`.
Dependency cycles are detected and skipped, never hung.

**Sharing.** Set `share: true` on an order to publish its result to the board
under its id, so dependents (and Claude) can read it. `run_agent` workers get
`read_board` / `write_board` / `list_agents` tools to do this live.

**Message bus.** Beyond the blackboard (publish *facts*), there's a directed +
broadcast message bus (`.delegate/messages.jsonl`):

- Worker tools: `post_message(text, to?)` (omit `to` to broadcast) and
  `read_messages()` (returns only what's new for this agent, via a per-agent
  cursor in the registry). So agent A can ask agent B something and B picks it up
  mid-task.
- Orchestrator/human tools: `send_message(work_dir, text, to?)` to steer or
  answer a running agent, and `read_messages(work_dir, agent?, since?)` to read
  the conversation. The viewer's message box posts here too.

A named agent sees broadcasts + messages addressed to/from it; the orchestrator
(`agent=""`) sees everything.

**Hooks.** Built-in lifecycle events always fire (registry + event log). The
`delegate_run` report returns the board, registry, and event tail so Claude can
react after the run (orchestrator notification). For side effects, pass
`hooks: {on_start, on_finish, on_fail}` — shell templates run at each lifecycle
point, with `{id}/{status}/{output_path}/{error}` substituted (shell-quoted),
subject to the same `allow_commands` + denylist as everything else.

```jsonc
{
  "orders": [
    { "id": "schema", "prompt": "Design the JSON schema for X. Return ONLY JSON.",
      "validate": { "type": "json" }, "share": true },
    { "id": "impl", "prompt": "Implement X against the schema above.",
      "depends_on": ["schema"], "output_path": "src/x.py",
      "validate": { "type": "shell", "cmd": "python -c \"import ast; ast.parse(open('src/x.py').read())\"" } }
  ],
  "work_dir": "/path/to/project",
  "allow_commands": ["python -c"],
  "hooks": { "on_fail": "echo failed {id} >> .delegate/failures.log" }
}
```

## Localhost viewer

A dependency-free (stdlib only) window into a live run, and your channel to talk
to the agents:

```bash
.venv/bin/python viewer.py <work_dir> [port]   # default http://127.0.0.1:7878
```

It reads the same `.delegate/` files the MCP writes (run it as a separate
process) and auto-refreshes every 1.5s:

- **Agents** — the roster with live status, attempts, output paths, errors
- **Blackboard** — everything agents have published
- **Events** — the lifecycle timeline
- **Spend** — running USD + call count
- **Message box** — what you type goes onto the message bus (as `human`); agents
  read it via `read_messages` and reply via `post_message`. Bidirectional
  human↔agent chat, with directed messages shown as `from→to`.

## Retrieval — keeping discovery cheap

When an agent doesn't know where something is, naive grep + whole-file reads are
where context tokens go to die (every fat result also gets re-sent each step
until compaction). Two layers keep it cheap:

**Orchestrator-first (the cheapest path, no worker search at all).** Claude — the
smart layer — locates code with a real index and inlines the exact spans into the
work order, so the cheap worker never explores. If a `clean` code-index MCP is
available, the playbook is: `index_repo` once → `search_code` (semantic+keyword,
token-compact TOON output) → `get_source`/`expand_result` for the precise span →
paste it into the order's `prompt`. The worker spends ~0 retrieval tokens.

**Worker fallback (autonomous `run_agent` on an un-indexed dir).** The built-in
tools are cheap by default:
- `read_file(path, offset, limit)` returns a **windowed**, line-numbered slice
  (default `DELEGATE_READ_LINES`=250) with a "read offset=N to continue" hint —
  never a whole-file dump.
- `grep(pattern, content?, path?)` uses ripgrep (gitignore-aware) and returns
  **matching files + hit counts by default**; the agent opts into actual lines
  with `content=true` on the one file it cares about. Falls back to a pure-Python
  scan (skipping `.venv`/`node_modules`/…) if `rg` isn't installed.

## Reliability

Real runs hit rate limits and flaky upstreams; the client handles it so a wide
`delegate_run` doesn't half-fail:

- **Concurrency cap** — a global semaphore limits simultaneous upstream calls
  across every tool (`DELEGATE_MAX_CONCURRENCY`, default 8), so 100 ready orders
  don't open 100 sockets.
- **Backoff + retry** — transient errors (429 / 5xx / timeout / transport) retry
  with exponential backoff + jitter, honoring `Retry-After`
  (`DELEGATE_RETRIES`=4, `DELEGATE_TIMEOUT`=180s). Non-transient errors (400/401/
  404) fail fast.
- **Model fallback** — `delegate_run(fallback=...)` or a per-order `fallback`
  retries a stuck order once on a different model.

## Context compaction

`run_agent` transcripts grow every step and overflow a cheap model's window. The
trigger is **auto-derived from the worker model's real context window** — 75%
(`DELEGATE_COMPACT_RATIO`) of it — so a 1M-window model (gemini-flash → ~750k)
isn't trimmed at the same point as a 32k one (qwen → ~24k). Unknown models fall
back to `DELEGATE_DEFAULT_CONTEXT` (32k); `DELEGATE_CONTEXT_BUDGET` hard-overrides
if you ever need a fixed number.

When the transcript crosses the budget, the oldest turns are summarized into one
note and the last `DELEGATE_KEEP_SEGMENTS` (4) turns kept verbatim. Compaction
happens at turn boundaries, so an assistant tool-call is never split from its
responses. Each compaction logs a `compact` event.

## Safety rails

- Every file path is confined to `work_dir`; escapes are rejected.
- Shell execution (`run_command`, shell validators) is **off** unless the caller
  passes `allow_commands` prefixes. A hard denylist blocks `rm -rf`, `sudo`,
  `curl`, etc. regardless.
- The worker never touches disk outside `work_dir`.

## Cost tracking

Every worker call appends `{model, tokens, usd}` to
`work_dir/.delegate/ledger.json`. `get_spend(work_dir)` aggregates it so Claude
can report real spend and decide when delegating stops paying off.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then add your OPENROUTER_API_KEY
.venv/bin/python -m pytest tests/ -q   # 104 network-free tests
```

Registered in `~/.claude.json` as the `delegate` MCP. **Add your
`OPENROUTER_API_KEY`** to that entry's `env` (currently empty) to make live calls
work; until then every tool returns a clear "key not set" error.

## Layout

```
delegate_mcp/
├── server.py          # thin MCP surface: tool wrappers + FastMCP entrypoint
├── store.py           # lock + .delegate/ path helpers
├── ledger.py          # model catalog, pricing, spend ledger
├── sandbox.py         # path confinement + command denylist
├── coordination.py    # blackboard, registry, events, hooks, DAG scheduler
├── validators.py      # deterministic output gates
├── edits.py           # surgical str-replace edit core (shared)
├── messages.py        # directed/broadcast message bus
├── workers.py         # OpenRouter client + config (concurrency cap, backoff, fallback)
├── compaction.py      # run_agent transcript compaction
├── delegate.py        # the autonomous DAG loop
├── agent.py           # the sandboxed tool-calling worker
├── viewer.py          # stdlib localhost viewer + human<->agent message feed
├── requirements.txt
├── .env.example
├── tests/
│   ├── test_logic.py  # pricing, ledger, validators, sandbox
│   ├── test_coord.py  # board, registry, events, hooks, DAG scheduler
│   ├── test_edits.py  # surgical edit core
│   ├── test_reliability.py  # retry/backoff + model fallback
│   ├── test_compaction.py   # transcript segmentation + compaction
│   ├── test_retrieval.py    # ranged read_file + names-first grep
│   ├── test_messages.py     # message bus (directed/broadcast/since)
│   └── test_delegate.py     # orchestrator DAG pre-flight
└── README.md
```

Each module is independent enough to rewrite on its own: the pure libraries
(`store`, `ledger`, `sandbox`, `validators`, `coordination`) have no MCP or
network dependencies; `workers`/`delegate`/`agent` are the I/O layers; `server.py`
only wires tools to them.
