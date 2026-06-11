# Harness backend API — frontend contract

Base URL: `http://127.0.0.1:8787` (configurable). Transport: **REST + SSE**.
All bodies are JSON. The backend is the brain; the browser is a thin client.

## Concepts

- **Session** — one conversation with an orchestrator (a strong model) bound to a
  working directory (`cwd`). The orchestrator plans and spawns cheap workers.
- **Event stream** — every session has an append-only event log. The browser
  subscribes via SSE and also gets a full snapshot via REST on load.
- **Provider/model** — chosen per session: `{provider: "openrouter"|"anthropic", model: "..."}`.

## REST

| Method | Path | Body / returns |
|---|---|---|
| `POST` | `/api/sessions` | `{cwd, title?, provider?, model?, mode?}` → `{id, ...session}` (`mode`: `solo` = single agent, `delegate` = +workers) |
| `GET` | `/api/sessions` | → `[{id, title, cwd, status, ...}]` |
| `GET` | `/api/sessions/{id}` | → full session `{id, messages, status, provider, model, cwd}` |
| `DELETE` | `/api/sessions/{id}` | → `{ok}` |
| `POST` | `/api/sessions/{id}/message` | `{text}` → `{ok}` — user turn; orchestrator streams over SSE. **If `text` starts with `/` it's run as a command instead** (returns the command result inline). |
| `POST` | `/api/sessions/{id}/command` | `{text:"/compact"}` or `{name, args:[]}` → command result |
| `GET` | `/api/commands` | → `[{name, help}]` available slash commands |
| `POST` | `/api/sessions/{id}/interrupt` | → `{ok}` — stop the current run |
| `POST` | `/api/sessions/{id}/approve` | `{request_id, approved, note?}` → `{ok}` — answer a permission request |
| `GET` | `/api/sessions/{id}/events?since={seq}` | → `[event]` snapshot/replay |
| `GET` | `/api/sessions/{id}/board` | → blackboard dict |
| `GET` | `/api/sessions/{id}/agents` | → worker roster |
| `GET` | `/api/sessions/{id}/messages?agent=&since=` | → message bus |
| `POST` | `/api/sessions/{id}/messages` | `{text, to?}` → `{seq}` — human → agent/orchestrator |
| `GET` | `/api/sessions/{id}/spend` | → `{usd, calls, by_model, ...}` |
| `GET` | `/api/models` | → curated worker models + provider info |

## SSE

`GET /api/sessions/{id}/stream` — `text/event-stream`. Each SSE `data:` line is one
event object. Reconnect with `Last-Event-ID` (the event `seq`) to resume; the
server replays missed events. Event shape:

```jsonc
{ "seq": 12, "ts": 1780000000.0, "type": "<type>", ... }
```

Event types (server → browser):

| type | fields | meaning |
|---|---|---|
| `status` | `state` (idle\|thinking\|running\|waiting_approval\|error) | run lifecycle |
| `token` | `text` | streamed assistant token delta |
| `message` | `role`, `content` | a complete message committed to the transcript |
| `tool_call` | `id`, `name`, `args` | orchestrator invoked a tool |
| `tool_result` | `id`, `result` | that tool returned |
| `approval_request` | `request_id`, `action`, `detail` | needs human approve/deny |
| `agent` | `agent_id`, `status`, `task` | worker roster changed |
| `board` | `key` | blackboard updated |
| `chat` | `from`, `to`, `text` | message-bus message |
| `spend` | `usd`, `delta` | running cost update |
| `error` | `error` | something failed |
| `done` | `stop_reason` | the turn finished |

## Client → server (over REST, not SSE)

- send a user turn: `POST /message {text}`
- steer mid-run / talk to a worker: `POST /messages {text, to?}`
- interrupt: `POST /interrupt`
- answer a permission prompt: `POST /approve {request_id, approved, note?}`

## Commands (harness-side, not the LLM)

Slash commands are implemented by the backend — the model provider gives you none
of these. Send them as a `/`-prefixed message or via `POST /command`. The result
also arrives on the SSE stream as a `command` event `{name, ok, result}`.

| command | effect |
|---|---|
| `/help` | list commands |
| `/compact [keep]` | summarize older turns, shrink the transcript (keep last N) |
| `/clear` | wipe the transcript |
| `/model [provider] <model>` | show/set the brain's provider+model |
| `/mode solo\|delegate` | single-agent vs orchestrator-with-workers |
| `/cost` | worker spend for this session |
| `/title <text>` | rename the session |

## Notes for the frontend

- On session open: `GET /api/sessions/{id}` for the transcript, then open the SSE
  stream, then lazy-load `/board` `/agents` `/messages` `/spend` for side panels
  (they also update live via `board`/`agent`/`chat`/`spend` events).
- `token` events are deltas — concatenate until the matching `message` commits.
- `approval_request` blocks the run until you `POST /approve`.
