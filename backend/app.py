"""FastAPI app: REST + SSE surface for the harness. See CONTRACT.md."""

import os
import sys
import json
import asyncio
import pathlib

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import ledger  # noqa: E402
import coordination as coord  # noqa: E402
import messages as msgbus  # noqa: E402

from . import session as sessions, orchestrator, providers, commands

app = FastAPI(title="delegate-harness")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


def _require(sid):
    s = sessions.get(sid)
    if not s:
        raise HTTPException(404, "session not found")
    return s


# ------------------------- sessions -------------------------


@app.post("/api/sessions")
async def create_session(body: dict):
    cwd = body.get("cwd") or os.getcwd()
    if not pathlib.Path(cwd).expanduser().is_dir():
        raise HTTPException(400, f"cwd not a directory: {cwd}")
    s = sessions.create(
        cwd,
        body.get("title", ""),
        body.get("provider", "openrouter"),
        body.get("model", ""),
        body.get("mode", "delegate"),
    )
    return s.to_dict()


@app.get("/api/sessions")
async def list_sessions():
    return sessions.list_all()


@app.get("/api/sessions/{sid}")
async def get_session(sid: str):
    return _require(sid).to_dict()


@app.delete("/api/sessions/{sid}")
async def delete_session(sid: str):
    return {"ok": sessions.delete(sid)}


# ------------------------- turn control -------------------------


@app.post("/api/sessions/{sid}/message")
async def post_message(sid: str, body: dict):
    s = _require(sid)
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "empty message")
    if commands.is_command(
        text
    ):  # /compact, /clear, /model, ... handled here, not by the model
        return await commands.run(s, text)
    if orchestrator.is_running(sid):
        raise HTTPException(409, "a turn is already running")
    asyncio.create_task(orchestrator.run_turn(s, text))
    return {"ok": True}


@app.post("/api/sessions/{sid}/command")
async def post_command(sid: str, body: dict):
    """Run a slash command explicitly. Accepts {text:"/compact"} or {name, args:[]}."""
    s = _require(sid)
    text = body.get("text")
    if not text:
        name = body.get("name", "")
        text = (
            "/"
            + name
            + (" " + " ".join(body.get("args", [])) if body.get("args") else "")
        )
    return await commands.run(s, text)


@app.get("/api/commands")
async def list_commands():
    return commands.catalog()


@app.post("/api/sessions/{sid}/interrupt")
async def interrupt(sid: str):
    orchestrator.interrupt(sid)
    return {"ok": True}


@app.post("/api/sessions/{sid}/approve")
async def approve(sid: str, body: dict):
    ok = orchestrator.resolve_approval(
        sid, body.get("request_id", ""), body.get("approved", True)
    )
    return {"ok": ok}


# ------------------------- live stream -------------------------


@app.get("/api/sessions/{sid}/stream")
async def stream(sid: str, request: Request):
    _require(sid)
    since = int(
        request.headers.get("last-event-id") or request.query_params.get("since") or 0
    )
    q = sessions.subscribe(sid)

    async def gen():
        try:
            for e in sessions.read_events(sid, since=since):
                yield {"id": str(e["seq"]), "data": json.dumps(e)}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    e = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue
                yield {"id": str(e["seq"]), "data": json.dumps(e)}
        finally:
            sessions.unsubscribe(sid, q)

    return EventSourceResponse(gen())


# ------------------------- state panels -------------------------


@app.get("/api/sessions/{sid}/events")
async def events(sid: str, since: int = 0):
    _require(sid)
    return sessions.read_events(sid, since=since)


@app.get("/api/sessions/{sid}/board")
async def board(sid: str):
    return coord.board_get(_require(sid).cwd) or {}


@app.get("/api/sessions/{sid}/agents")
async def agents(sid: str):
    return coord.reg_get(_require(sid).cwd)


@app.get("/api/sessions/{sid}/messages")
async def get_messages(sid: str, agent: str = "", since: int = 0):
    return msgbus.read_messages(_require(sid).cwd, agent, since)


@app.post("/api/sessions/{sid}/messages")
async def post_bus_message(sid: str, body: dict):
    s = _require(sid)
    seq = msgbus.post_message(s.cwd, "human", body.get("text", ""), body.get("to", ""))
    return {"seq": seq}


@app.get("/api/sessions/{sid}/spend")
async def spend(sid: str):
    return ledger.spend_summary(_require(sid).cwd)


@app.get("/api/models")
async def models():
    return {
        "providers": list(providers.PROVIDERS),
        "default_models": providers.DEFAULT_MODELS,
        "workers": [{"id": m[0], "price": m[1], "note": m[2]} for m in ledger.MODELS],
    }


@app.get("/api/health")
async def health():
    return {"ok": True}
