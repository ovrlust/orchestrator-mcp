"""Worker model catalog, pricing, and the per-work_dir spend ledger."""

import json
import pathlib

from store import LOCK, read_json

# (model id, "$in / $out" per 1M tokens, note, in_price, out_price)
MODELS = [
    (
        "openai/gpt-4o-mini",
        "$0.15 / $0.60",
        "cheap reliable general worker (tool-calling)",
        0.15,
        0.60,
    ),
    (
        "google/gemini-2.0-flash-001",
        "$0.10 / $0.40",
        "fast, very cheap, long context (tool-calling)",
        0.10,
        0.40,
    ),
    (
        "deepseek/deepseek-chat",
        "$0.27 / $1.10",
        "strong at code, cheap (tool-calling)",
        0.27,
        1.10,
    ),
    (
        "qwen/qwen-2.5-72b-instruct",
        "$0.12 / $0.39",
        "cheap, good structured output (tool-calling)",
        0.12,
        0.39,
    ),
    (
        "meta-llama/llama-3.3-70b-instruct",
        "$0.12 / $0.30",
        "open, very cheap bulk text",
        0.12,
        0.30,
    ),
]
PRICES = {m[0]: (m[3], m[4]) for m in MODELS}


def cost_usd(model: str, usage: dict) -> float:
    """USD cost of a single worker call from its token usage. 0 if model unknown."""
    pin, pout = PRICES.get(model, (0.0, 0.0))
    pt = usage.get("prompt_tokens", 0) or 0
    ct = usage.get("completion_tokens", 0) or 0
    return round(pt / 1_000_000 * pin + ct / 1_000_000 * pout, 6)


def ledger_path(work: str) -> pathlib.Path:
    return pathlib.Path(work) / ".delegate" / "ledger.json"


def record_spend(work: str, model: str, usage: dict) -> float:
    """Append one worker call to the ledger; returns the call's USD cost."""
    usd = cost_usd(model, usage)
    p = ledger_path(work)
    with LOCK:
        p.parent.mkdir(parents=True, exist_ok=True)
        led = read_json(p, [])
        led.append(
            {
                "model": model,
                "prompt_tokens": usage.get("prompt_tokens", 0) or 0,
                "completion_tokens": usage.get("completion_tokens", 0) or 0,
                "usd": usd,
            }
        )
        p.write_text(json.dumps(led, indent=2))
    return usd


def spend_summary(work: str) -> dict:
    """Aggregate the ledger for a work_dir."""
    p = ledger_path(work)
    if not p.exists():
        return {
            "calls": 0,
            "usd": 0.0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "by_model": {},
        }
    led = read_json(p, None)
    if led is None:
        return {"error": "ledger unreadable", "path": str(p)}
    by_model: dict = {}
    usd = pt = ct = 0
    for e in led:
        usd += e.get("usd", 0.0)
        pt += e.get("prompt_tokens", 0)
        ct += e.get("completion_tokens", 0)
        m = e.get("model", "?")
        bm = by_model.setdefault(m, {"calls": 0, "usd": 0.0})
        bm["calls"] += 1
        bm["usd"] = round(bm["usd"] + e.get("usd", 0.0), 6)
    return {
        "calls": len(led),
        "usd": round(usd, 6),
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "by_model": by_model,
    }
