"""The orchestrator: a provider-agnostic streaming tool-loop that IS the harness
brain. It plans, edits, searches, delegates to cheap workers, and talks to the
user — emitting every token/tool-call/result as a session event for SSE.
"""

import os
import sys
import asyncio
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import ledger  # noqa: E402
import coordination as coord  # noqa: E402
import messages as msgbus  # noqa: E402

from . import providers, tools, session as sessions

MAX_ITERS = int(os.environ.get("HARNESS_MAX_ITERS", "50"))
# Secure default: command/delegate calls wait for /approve. Set
# HARNESS_AUTO_APPROVE=1 to run unattended.
AUTO_APPROVE = os.environ.get("HARNESS_AUTO_APPROVE", "0") == "1"
APPROVAL_TIMEOUT = float(os.environ.get("HARNESS_APPROVAL_TIMEOUT", "300"))
NEEDS_APPROVAL = {"run_command", "delegate", "spawn_agent"}

_interrupts: dict = {}  # sid -> asyncio.Event
_approvals: dict = {}  # sid -> {request_id: asyncio.Future}
_running: dict = {}  # sid -> bool

_BASE = (
    "You are a coding agent working in the user's project directory. You own the outcome. You can "
    "read/edit files (edit_file for surgical changes — read the file first), search (grep is ripgrep, "
    "names-first), run allowlisted commands, and track work on the shared board / message bus. Keep "
    "the user informed in plain prose. Act; don't ask permission for routine reads/edits."
)
_DELEGATE = (
    "\n\nYour edge is delegation: for high-volume, fully-specifiable grind (bulk edits, translations, "
    "boilerplate, repetitive transforms) decompose it into orders with a validator each and call "
    "`delegate` to run them on cheap workers; for a self-contained sub-task that needs its own "
    "exploration loop, use `spawn_agent`. Do the planning and validation yourself; push the grind down."
)


def _system(mode):
    return _BASE + (_DELEGATE if mode != "solo" else "")


def interrupt(sid):
    ev = _interrupts.get(sid)
    if ev:
        ev.set()


def is_running(sid):
    return _running.get(sid, False)


def resolve_approval(sid, request_id, approved):
    fut = _approvals.get(sid, {}).get(request_id)
    if fut and not fut.done():
        fut.set_result(bool(approved))
        return True
    return False


async def _request_approval(session, call):
    if AUTO_APPROVE:
        return True
    rid = f"{call['id']}"
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    _approvals.setdefault(session.id, {})[rid] = fut
    sessions.append_event(
        session.id,
        {
            "type": "approval_request",
            "request_id": rid,
            "action": call["name"],
            "detail": call.get("args", {}),
        },
    )
    sessions.append_event(session.id, {"type": "status", "state": "waiting_approval"})
    try:
        return await asyncio.wait_for(fut, timeout=APPROVAL_TIMEOUT)
    except asyncio.TimeoutError:
        return False
    finally:
        _approvals.get(session.id, {}).pop(rid, None)


async def _bridge(session, stop_ev):
    """Tail the worker coordination log + message bus and re-emit as session
    events, so the SSE stream stays live with worker/board/chat activity."""
    work, sid = session.cwd, session.id
    ev_count = len(coord.events_tail(work, 10000))
    seen = msgbus.read_messages(work)
    msg_seq = max((m["seq"] for m in seen), default=0)
    while not stop_ev.is_set():
        await asyncio.sleep(0.6)
        evs = coord.events_tail(work, 10000)
        for e in evs[ev_count:]:
            et = e.get("type")
            if et in ("start", "finish", "fail", "skip"):
                sessions.append_event(
                    sid,
                    {
                        "type": "agent",
                        "agent_id": e.get("agent"),
                        "status": e.get("status") or et,
                        "task": e.get("task", ""),
                    },
                )
            elif et == "board_set":
                sessions.append_event(sid, {"type": "board", "key": e.get("key")})
        ev_count = len(evs)
        for m in msgbus.read_messages(work, since=msg_seq):
            sessions.append_event(
                sid,
                {
                    "type": "chat",
                    "from": m["from"],
                    "to": m.get("to", ""),
                    "text": m["text"],
                },
            )
            msg_seq = max(msg_seq, m["seq"])


async def run_turn(session, user_text: str):
    """Run one user turn to completion, streaming events. Persists as it goes."""
    sid = session.id
    _running[sid] = True
    _interrupts[sid] = asyncio.Event()
    emit = lambda **e: sessions.append_event(sid, e)  # noqa: E731
    ctx = {
        "work": session.cwd,
        "allow_cmds": list(getattr(session, "allow_commands", []) or []),
        "seen": set(),
        "changed": set(),
        "model": "",
    }  # worker model: provider default

    session.messages.append({"role": "user", "content": user_text})
    session.status = "thinking"
    sessions.save(session)
    emit(type="status", state="thinking")

    bridge = asyncio.create_task(_bridge(session, _interrupts[sid]))
    stop = "stop"
    try:
        for _ in range(MAX_ITERS):
            if _interrupts[sid].is_set():
                stop = "interrupted"
                break
            text_parts, calls = [], []
            msgs = [
                {"role": "system", "content": _system(session.mode)}
            ] + session.messages
            gen = providers.stream(
                session.provider, msgs, tools.toolset(session.mode), session.model
            )
            async for ev in gen:
                if _interrupts[sid].is_set():
                    break
                t = ev["type"]
                if t == "text":
                    text_parts.append(ev["text"])
                    emit(type="token", text=ev["text"])
                elif t == "tool_call":
                    calls.append(ev)
                elif t == "usage":
                    usd = ledger.record_spend(
                        session.cwd, session.model or "orchestrator", ev
                    )
                    emit(
                        type="spend",
                        usd=ledger.spend_summary(session.cwd).get("usd", 0.0),
                        delta=usd,
                    )
                elif t == "error":
                    emit(type="error", error=ev["error"])
                    session.status = "error"
                    sessions.save(session)
                    emit(type="status", state="error")
                    return
                elif t == "done":
                    stop = ev.get("stop_reason", stop)

            text = "".join(text_parts)
            amsg = {"role": "assistant", "content": text or None}
            if calls:
                amsg["tool_calls"] = [
                    {"id": c["id"], "name": c["name"], "args": c["args"]} for c in calls
                ]
            session.messages.append(amsg)
            sessions.save(session)
            if text:
                emit(type="message", role="assistant", content=text)

            if not calls or _interrupts[sid].is_set():
                if _interrupts[sid].is_set():
                    stop = "interrupted"
                break

            emit(type="status", state="running")
            for c in calls:
                emit(type="tool_call", id=c["id"], name=c["name"], args=c["args"])
                if c["name"] in NEEDS_APPROVAL and not await _request_approval(
                    session, c
                ):
                    result = "DENIED by user"
                else:
                    try:
                        result = await tools.dispatch(c["name"], c["args"], ctx)
                    except Exception as e:  # noqa: BLE001
                        result = f"ERROR: {type(e).__name__}: {e}"
                emit(type="tool_result", id=c["id"], result=str(result)[:2000])
                session.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": c["id"],
                        "name": c["name"],
                        "content": str(result),
                    }
                )
            sessions.save(session)
    finally:
        bridge.cancel()
        _running[sid] = False
        session.status = "idle"
        sessions.save(session)
        emit(type="status", state="idle")
        emit(type="done", stop_reason=stop)
