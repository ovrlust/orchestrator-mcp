# orchestrator

An MCP server for the **orchestrator → worker** pattern.

Claude (smart, expensive) **plans and validates**. A cheap external model (the
*worker*, via [OpenRouter](https://openrouter.ai)) **executes**. The point is to
push high-volume, fully-specifiable grind onto the cheap model while Claude keeps
the judgment — and to do it without Claude babysitting every unit.

(The standalone browser harness that used to live in `backend/` is now its own
project: [orchestrator-harness](../orchestrator-harness). It imports this project's
modules via `DELEGATE_MCP_PATH`.)

## Tools

| Tool | What it does |
|------|--------------|
| `ask_model` | One stateless work order to a worker. `{text, model, usage}` |
| `ask_model_batch` | Many independent orders, concurrently |
| `delegate_run` | **The autonomous loop.** A DAG of orders → worker → apply → validate → retry-once → rollback-on-fail → share → report |
| `map_files` | **Bulk transform.** One instruction applied to every file matching a glob, fanned out across workers (read → transform → validate → rollback-on-fail). `dry_run` previews matches |
| `run_agent` | A worker as a sandboxed, board-aware tool-calling agent (incl. surgical `edit_file`) inside `work_dir`. `agent_type` presets: `general` / `explore` (read-only scout) / `plan`; `output_schema` forces validated JSON results |
| `spawn_agent` | Same agent, but in the BACKGROUND — returns an id immediately; fan out several in parallel |
| `agent_result` | Poll or wait for a spawned agent's result |
| `agent_send` | Continue any agent with a follow-up message, its full transcript intact (live agents get it pushed mid-run) |
| `agent_stop` | Cancel a running spawned agent; its last per-step checkpoint stays resumable |
| `direct` | Director: split a plan into sections, each run by its OWN agent in parallel (deps respected) |
| `supervise` | Same dispatch as `direct`, plus a supervisor model polling live state that can message agents or stop the run |
| `board_read` / `board_write` | Read/seed the shared blackboard |
| `send_message` / `read_messages` | Talk to agents (directed or broadcast) over the message bus |
| `agents` | The live agent roster (who exists, task, status, files) |
| `monitor` | One live view of a run: roster + events + board keys + messages |
| `events` | Tail the lifecycle event log |
| `tool_log` | Per-tool-call audit log for `run_agent` workers (filter by agent/fn/errors) |
| `coord_reset` | Wipe board/registry/events/messages for a fresh run (ledger kept) |
| `understand_project` | Scan a repo once into a cached structural map (incremental, no model calls) |
| `project_context` / `project_overview` | Read the cached map / a free architecture digest |
| `summarize_project` | Opt-in cheap-LLM 1-line role summary per file (incremental) |
| `list_models` | Curated cheap workers with prices |
| `get_spend` | Total worker spend logged for a `work_dir` (tokens + USD) |
| `cache_stats` | Inspect/clear the deterministic worker result cache |

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

## Sub-agents (run_agent / spawn_agent / agent_result / agent_send)

Sub-agents work like Claude's native Agent tool, on cheap models:

- **Presets** (`agent_type`): `explore` is a read-only scout (find → read windows
  → return compressed findings with `path:line` refs; cannot edit or run
  commands — enforced server-side, not just by prompt). `plan` is read-only +
  `write_board`, returns an implementation plan. `skeptic` is a read-only
  adversarial verifier — give it a claim/finding and it tries to REFUTE it
  against the code, returning verified/refuted/uncertain + evidence ($0
  double-check for a worker's output). `general` (default) is the full executor.
  Every type gets the REPORT CONTRACT: its `done(summary)` is a return value for
  the orchestrator, not chat.
- **Structured output** (`output_schema`): the final summary must be JSON
  matching your schema — validated server-side, rejections fed back to the agent
  (bounded retries), `result` returned as the parsed object.
- **Background** (`spawn_agent`): returns `{agent_id}` immediately; the agent
  runs inside the MCP server process. Watch live progress with `monitor`,
  collect with `agent_result` (poll or wait), fan out several in parallel.
- **Resume** (`agent_send`): every run's full transcript is persisted to
  `work_dir/.delegate/agents/<id>.json`. Send a follow-up to a FINISHED agent
  and it continues with its whole context intact (same model, same allowlist,
  same type). Send to a RUNNING agent and the message is pushed into its loop
  mid-run.

Typical economics: spawn an `explore` scout to sweep a codebase question, keep
working, collect a 1-page report — the cheap model burned its context on the
files so the orchestrator doesn't have to.

## Driving sub-agents: the orchestration playbook

Measured on the default worker (deepseek-v4-flash via opencode zen): it is a
**strong reasoner but a weak agentic-loop driver**. Given a loose task it
wanders — one real run burned 15 steps / 182k prompt tokens and never called
`done()`. The same question, asked correctly, finished in 8 steps / 38k tokens
with a line-accurate answer. The difference was entirely the orchestrator's
prompt.

**Direction of labor: the cheap model reads, the orchestrator navigates.** Push
bounded reading and grind DOWN to workers so their context absorbs the file
bulk, not yours; keep all navigation, judgment, and anything needing the live
conversation. If you can't name the files or state the success check, the task
isn't ready to delegate — scope it first.

Three rules when you (the master agent) write a sub-agent task:

1. **Scope the reads.** Name the files when you can ("read ONLY messages.py and
   agent.py's push-delivery loop"). An open-ended "investigate X" invites a
   whole-repo crawl.
2. **Demand convergence.** The presets now instruct "call done() the moment you
   can answer", but reinforce it in the task for anything broad.
3. **Force a schema.** Pass `output_schema` for anything you'll consume
   programmatically — a typed `done` is what reliably stops exploration and
   returns a parsed object instead of rambling prose.

Pick the tool by task shape:

| Task | Use |
|------|-----|
| Gather codebase context without bloating yourself | `spawn_agent` / `run_agent`, `agent_type=explore`, named files + a schema |
| One fully-specified mechanical edit | `run_agent` (`general`), exact file + change |
| Many independent stateless orders (bulk transform) | `delegate_run`, one validator each |
| Parallel sections that coordinate | `direct` / `supervise` |
| Anything needing judgment or this conversation's context | do it yourself |

Rule of thumb: delegate when the output is much smaller than the input (read 100
files → a 20-line answer) AND the task is bounded.

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
- Shell execution (`run_command`, shell validators, hooks) is **off** unless the
  caller passes `allow_commands` prefixes, matched on a token boundary. Shell
  chaining/redirection (`;`, `&&`, `|`, `>`, `` ` ``, `$(`) is rejected whenever
  an allowlist is in force, and a hard denylist blocks `rm -rf`, `sudo`, `curl`,
  etc. regardless.
- The worker never touches disk outside `work_dir`.
- These rails are best-effort protection against accidents, **not a security
  boundary** — keep allowlists tight and don't run untrusted prompts with broad
  ones.
- The worker's `fetch_url` / `web_search` / `download` tools are unrestricted
  network egress (the shell denylist on `curl`/`wget` does not gate them);
  `download` writes only inside `work_dir`.

## Cost tracking

Every worker call appends `{model, tokens, usd}` to
`work_dir/.delegate/ledger.json`. `get_spend(work_dir)` aggregates it so Claude
can report real spend and decide when delegating stops paying off.
(`ask_model` / `ask_model_batch` take no `work_dir` by nature — pass one
explicitly to have their calls logged too.)

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then add your OPENROUTER_API_KEY (loaded at startup)
.venv/bin/python -m pytest tests/ -q   # network-free test suite
```

Registered in `~/.claude.json` as the `delegate` MCP. **Add your
`OPENROUTER_API_KEY`** to that entry's `env` (currently empty) to make live calls
work; until then every tool returns a clear "key not set" error.

## Layout

```
orchestrator_mcp/
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
├── mapfiles.py        # bulk transform: glob → per-file worker orders
├── agent.py           # the sandboxed tool-calling worker
├── presets.py         # agent_type presets: prompts + tool subsets
├── subagents.py       # persistence, background spawn, resume
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
