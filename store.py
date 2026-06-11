"""Low-level shared primitives for the .delegate/ state dir.

Everything that does read-modify-write on a shared JSON/JSONL file goes through
the one process-wide LOCK so concurrent agents (one asyncio loop + worker
threads) never corrupt each other's writes.
"""

import json
import pathlib
import threading

LOCK = threading.Lock()


def coord_file(work: str, name: str) -> pathlib.Path:
    """Path to a file inside <work_dir>/.delegate/."""
    return pathlib.Path(work) / ".delegate" / name


def read_json(p: pathlib.Path, default):
    """Read JSON, returning `default` if missing or corrupt (never raises)."""
    try:
        return json.loads(p.read_text()) if p.exists() else default
    except Exception:  # noqa: BLE001 - a corrupt state file must not break a run
        return default
