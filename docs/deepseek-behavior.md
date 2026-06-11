# DeepSeek worker behavior

How the default worker model (`deepseek-v4-flash-free` via opencode zen) actually
behaves inside the delegate agent loop, what it's good and bad at, and the
mechanisms — beyond prompting — that keep it from drifting.

All numbers below are from real runs against the live endpoint in this repo, not
estimates. Re-measure if the model or endpoint changes.

## One-line profile

**Strong reasoner, weak agentic-loop driver.** It reasons well about code in a
single shot, but left to run a multi-step tool loop on a loose task it
over-explores and forgets to finish. The orchestrator's job is to supply the
discipline the model lacks: scope, a convergence signal, and a result shape.

## What it's good at

- **Single-shot code reasoning.** Given a bug description it identified the cause
  and the fix correctly in two sentences. Given "find where X is defined, list
  the tool calls" it produced the minimal, correct sequence.
- **Line-accurate tracing when scoped.** Asked to trace the message lifecycle
  with the files named, it returned `_last_seq`, push-delivery line range, and
  the dual cursor location — all correct to the line.
- **Structured output.** With `output_schema` it returns valid JSON; it also
  tolerates ```json fences (the loop strips them). Schema-forcing is the single
  most reliable way to make it stop and answer.
- **Clean give-up.** Told "if it doesn't exist, say so and call done
  immediately," it did — 2 steps, ~3.4k tokens. It does not loop forever on an
  impossible task *when the task says how to bail*.

## What it's bad at

- **Converging on its own.** This is the main failure. A loose task —
  "trace the message lifecycle" with no file scope — ran **15 steps / 182k
  prompt tokens and never called `done()`**, returning the empty
  "(max_steps reached)" sentinel. It completed its own `update_plan` checklist
  (6/6) and *still* didn't recognize "plan done → finish." It keeps gathering
  for completeness.
- **Scope control.** Unbounded, it reads adjacent/irrelevant files "to be
  thorough" (in the 182k run it read `viewer.py`, `director.py`, and grepped
  `@mcp.tool` — none relevant to a message trace).
- **Reasoning-token burn — and it's NOT controllable.** It is a reasoning model:
  a 2-sentence answer cost **7067 reasoning tokens**. The zen endpoint accepts
  `reasoning_effort`, `reasoning.enabled=false`, `reasoning.max_tokens`,
  `chat_template_kwargs.thinking=false` (all HTTP 200) but does NOT honor them —
  measured `reasoning.enabled=false` *increased* reasoning on a real task
  (3836→5119). Treat reasoning as a fixed, unavoidable cost of this model.
  - Reasoning tokens cost wall-clock and $ (on paid tiers) but **never enter the
    orchestrator's context** — they're internal. The only worker output that
    reaches you is the final answer, which IS controllable (see below).
  - No free zen model is zero-reasoning. `nemotron-3-ultra-free` is the lightest
    (27 reasoning tokens vs deepseek's 113 on a trivial transform) — route pure
    mechanical/bulk work there; keep deepseek for reasoning-worthy tasks.
  - `minimax-m3-free` and `qwen3.6-plus-free` free promos have ended (paid only);
    removed from the catalog.

### Minimizing the yap that reaches you

You can't cut the model's internal reasoning, but the **answer prose** it returns
is fully controllable and is the only part that bloats your context:

- The REPORT CONTRACT (in every preset) now bans preamble ("Now I have the full
  picture…"), sign-offs, process narration, pleasantries, and gratuitous
  markdown. Measured: answers now start with the first byte of content instead of
  a paragraph of throat-clearing.
- For the hardest guarantee, pass `output_schema` — the answer must be a JSON
  object with exactly the fields you want, so decoration is impossible.
- **Literal/positional instructions.** Asked for "the third word of this
  sentence" it answered `word` (actual: `valid`). Don't rely on it for exact
  counting, indexing, or character-level precision.
- **Wrapping file output in markdown fences.** Told to return a complete file, it
  wraps the answer in ```` ```python … ``` ````. Writing that verbatim corrupts
  the file (stray fences become real lines) — caught live: a `map_files` run
  "passed" its validator while leaving broken ``` lines in 2 of 3 files. The
  apply path now strips a single wrapping fence (`delegate._strip_code_fence`),
  and the bulk prompt forbids fences. Lesson: validate structure, and never trust
  whole-file rewrites from a cheap model without it.
- **Ignoring a restricted toolset.** On the forced final step, offered only the
  `done` tool, it still emitted a different tool call. Models hallucinate
  un-offered tools — so convergence can't rely on tool restriction alone (see
  the hard fallback below).

## The measured prompt effect

Same question ("trace a message's lifecycle"), same model, same step cap:

| Prompt style | Steps | Prompt tokens | Converged? | Answer |
|---|---|---|---|---|
| Loose ("trace the lifecycle") | 15 | 182,404 | no | lost |
| Hardened preset, still loose task | 12 | 119,912 | yes | correct |
| Tight (named files + `output_schema`) | 8 | 38,299 | yes | line-accurate |

The hardened `explore`/`plan` presets buy convergence (no more infinite wander).
Token economy on top of that is driven by the *orchestrator's task prompt* —
naming files and forcing a schema is what takes 120k → 38k.

## Anti-drift mechanisms (code, not prompt)

Prompting is the first lever; these are the structural backstops in the loop so a
bad prompt degrades gracefully instead of burning a run.

1. **Read-only preset enforcement** (`presets.py`, `agent.py`). `explore`/`plan`
   agents are offered only read tools AND a dispatch-time check rejects any
   write/exec call the model emits anyway. The model *cannot* edit under these
   types even if it tries.
2. **Forced convergence on the last step** (`agent.py`). On the final allowed
   step the loop offers only `done` and injects a "FINAL STEP — call done now"
   message.
3. **Hard finish fallback** (`agent.py`). Because the model may ignore (2), if it
   still hasn't finished after the last step the loop makes ONE no-tools call
   ("reply with your final answer as plain text") and returns that as an
   `incomplete` result. A starved run that used to return the empty
   "(max_steps reached)" sentinel now returns a real best-effort answer
   (verified: a 4-step architecture summary instead of nothing).
4. **Output-schema gate** (`agent.py`). With `output_schema`, `done` must be JSON
   matching the schema; rejections are fed back with bounded retries, and the
   final answer-extraction also re-validates. Forces a typed object out.
5. **Redundant-read guard** (`agent.py`). Re-reading the exact same file window
   returns a one-line "you already read this" note instead of re-serving the
   content (it's already in context above). The memory is invalidated when the
   file is written/edited, so a legitimate re-read after a change still works.
   Stops the model from paying twice for the same bytes.
6. **Soft token ceiling** (`agent.py`, `max_total_tokens`). A per-agent
   prompt+completion budget; crossing it forces the final step. Checked between
   steps, so it overshoots by ~one step + the final call — a runaway backstop for
   paid models, not an exact cap. (On the free tier, leave it at 0.)
7. **Per-step checkpointing** (`subagents.py`). The transcript is saved every
   step, so a crash or `agent_stop` mid-run is resumable from the last step
   rather than lost.
8. **Context compaction** (`compaction.py`). Older turns are summarized when the
   transcript nears the model's window, so a long loop doesn't overflow.
9. **Heartbeats + monitor** (`coordination.py`). Step and last-active are written
   to the registry each step, so a stalled agent is visible via `monitor` and can
   be steered (`agent_send`) or killed (`agent_stop`).
10. **Map injection** (`agent.py` → `project.overview_text`). Every agent's system
    prompt carries the cached structural digest (entrypoints, core modules, roles),
    built once and free. The model orients instead of burning steps rediscovering
    the layout. Measured: scouts dropped 3 steps → 2 and *fewer* prompt tokens
    despite the +1.5k digest — orientation paid for itself by skipping the
    discovery step.

## How to drive it (summary)

- The cheap model **reads**; the orchestrator **navigates**. Push bounded
  reading/grind down; keep judgment and live-conversation context up top.
- **Scope the reads** — name the files. **Demand convergence** — "call done the
  moment you can answer." **Force the shape** — pass `output_schema`.
- Delegate only when output ≪ input AND the task is bounded. If you can't name
  the files or the success check, scope it first; don't hand it down loose.

(Full playbook with the tool-by-task-shape table: README → "Driving sub-agents".)
