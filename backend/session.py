"""Sessions: persistence + the per-session event log and live pub/sub for SSE.

A session is one conversation with an orchestrator bound to a working directory.
Metadata + transcript persist to HARNESS_HOME/sessions/<id>.json; the append-only
event log lives alongside as <id>.events.jsonl. Live subscribers (SSE streams)
get events pushed via in-memory asyncio queues.
"""

import os
import json
import time
import uuid
import asyncio
import pathlib
import threading

HOME = pathlib.Path(
    os.environ.get("HARNESS_HOME", os.path.expanduser("~/.delegate-harness"))
)
SESS_DIR = HOME / "sessions"
_LOCK = threading.Lock()
_subs: dict = {}  # session_id -> set[asyncio.Queue]


def _now():
    return round(time.time(), 3)


def _path(sid):
    return SESS_DIR / f"{sid}.json"


def _events_path(sid):
    return SESS_DIR / f"{sid}.events.jsonl"


class Session:
    def __init__(
        self,
        sid,
        cwd,
        title="",
        provider="openrouter",
        model="",
        status="idle",
        messages=None,
        created=None,
        updated=None,
        mode="delegate",
    ):
        self.id = sid
        self.cwd = cwd
        self.title = title or "untitled"
        self.provider = provider
        self.model = model
        self.status = status
        self.messages = messages or []  # internal-format transcript
        self.created = created or _now()
        self.updated = updated or _now()
        # "solo" = single agent, no worker dispatch; "delegate" = + delegate/spawn_agent
        self.mode = mode if mode in ("solo", "delegate") else "delegate"

    def to_dict(self):
        return {
            "id": self.id,
            "cwd": self.cwd,
            "title": self.title,
            "provider": self.provider,
            "model": self.model,
            "status": self.status,
            "mode": self.mode,
            "messages": self.messages,
            "created": self.created,
            "updated": self.updated,
        }

    def summary(self):
        d = self.to_dict()
        d.pop("messages")
        return d


def create(cwd, title="", provider="openrouter", model="", mode="delegate"):
    sid = uuid.uuid4().hex[:12]
    s = Session(
        sid,
        str(pathlib.Path(cwd).expanduser().resolve()),
        title,
        provider,
        model,
        mode=mode,
    )
    save(s)
    return s


def save(session: Session):
    session.updated = _now()
    with _LOCK:
        SESS_DIR.mkdir(parents=True, exist_ok=True)
        _path(session.id).write_text(json.dumps(session.to_dict(), indent=2))


def get(sid) -> Session | None:
    p = _path(sid)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return None
    return Session(
        d["id"],
        d["cwd"],
        d.get("title"),
        d.get("provider"),
        d.get("model"),
        d.get("status", "idle"),
        d.get("messages"),
        d.get("created"),
        d.get("updated"),
        d.get("mode", "delegate"),
    )


def list_all() -> list:
    if not SESS_DIR.exists():
        return []
    out = []
    for p in SESS_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text())
            d.pop("messages", None)
            out.append(d)
        except Exception:  # noqa: BLE001
            pass
    return sorted(out, key=lambda x: x.get("updated", 0), reverse=True)


def delete(sid) -> bool:
    ok = False
    with _LOCK:
        for p in (_path(sid), _events_path(sid)):
            if p.exists():
                p.unlink()
                ok = True
    return ok


# ------------------------- events -------------------------


def append_event(sid, event: dict) -> dict:
    """Stamp an event with seq+ts, persist it, and push to live subscribers."""
    p = _events_path(sid)
    with _LOCK:
        SESS_DIR.mkdir(parents=True, exist_ok=True)
        seq = (sum(1 for _ in p.open(encoding="utf-8")) if p.exists() else 0) + 1
        rec = {"seq": seq, "ts": _now(), **event}
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    for q in list(_subs.get(sid, ())):
        try:
            q.put_nowait(rec)
        except Exception:  # noqa: BLE001
            pass
    return rec


def read_events(sid, since=0) -> list:
    p = _events_path(sid)
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            e = json.loads(ln)
        except Exception:  # noqa: BLE001
            continue
        if e.get("seq", 0) > since:
            out.append(e)
    return out


def subscribe(sid) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _subs.setdefault(sid, set()).add(q)
    return q


def unsubscribe(sid, q):
    subs = _subs.get(sid)
    if subs:
        subs.discard(q)
        if not subs:
            _subs.pop(sid, None)
