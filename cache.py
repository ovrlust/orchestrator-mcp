"""On-disk result cache for stateless worker calls.

Why: the whole point of delegating bulk/repeated work to cheap workers is cost
and speed. If the SAME fully-specified order is run twice — a re-run after a
crash, two delegate DAGs that share a sub-order, an idempotent batch — paying the
worker again is pure waste. A content-addressed cache turns the repeat into a
$0, instant hit.

Safety: only DETERMINISTIC calls are cached. A temperature>0 call is asking the
model to vary its answer, so returning a stale one would be wrong — those bypass
the cache entirely. Errors are never stored (so a transient 429 can't poison
future runs). The cache lives outside any work_dir (~/.delegate/cache) so it is
shared across projects; identical prompts hit regardless of where they run.

Disable with DELEGATE_CACHE=0. Relocate with DELEGATE_CACHE_DIR.
"""

import os
import json
import hashlib
import pathlib

ENABLED = os.environ.get("DELEGATE_CACHE", "1") != "0"

_env_dir = os.environ.get("DELEGATE_CACHE_DIR", "").strip()
CACHE_DIR = (
    pathlib.Path(_env_dir).expanduser()
    if _env_dir
    else pathlib.Path.home() / ".delegate" / "cache"
)


def cacheable(temperature) -> bool:
    """Only cache deterministic (temperature == 0) calls when caching is on."""
    try:
        return ENABLED and float(temperature) == 0.0
    except (TypeError, ValueError):
        return False


def _key(model: str, system: str, prompt: str, temperature, max_tokens) -> str:
    raw = json.dumps(
        [model, system, prompt, float(temperature), int(max_tokens or 0)],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _path(key: str) -> pathlib.Path:
    return CACHE_DIR / f"{key}.json"


def get(model, system, prompt, temperature, max_tokens):
    """Return a cached result for this exact request, or None on miss.

    The hit reports usage={} so cost accounting (record_spend) correctly charges
    $0, and carries cached=True so callers/tests can tell it came from cache.
    """
    if not cacheable(temperature):
        return None
    try:
        with open(_path(_key(model, system, prompt, temperature, max_tokens))) as f:
            stored = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return {
        "text": stored.get("text", ""),
        "model": stored.get("model", model),
        "usage": {},
        "cached": True,
    }


def put(model, system, prompt, temperature, max_tokens, result) -> None:
    """Store a successful result. No-ops for non-deterministic calls and errors."""
    if not cacheable(temperature):
        return
    if not isinstance(result, dict) or result.get("error") or not result.get("text"):
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _path(_key(model, system, prompt, temperature, max_tokens))
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump({"text": result["text"], "model": result.get("model", model)}, f)
        os.replace(tmp, path)  # atomic
    except OSError:
        pass  # cache is best-effort; never break a real call over it


def stats() -> dict:
    """{enabled, dir, entries, bytes} for the cache."""
    entries = 0
    total = 0
    if CACHE_DIR.is_dir():
        for p in CACHE_DIR.glob("*.json"):
            entries += 1
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return {
        "enabled": ENABLED,
        "dir": str(CACHE_DIR),
        "entries": entries,
        "bytes": total,
    }


def clear() -> dict:
    """Delete all cached results. Returns {removed}."""
    removed = 0
    if CACHE_DIR.is_dir():
        for p in CACHE_DIR.glob("*.json"):
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return {"removed": removed}
