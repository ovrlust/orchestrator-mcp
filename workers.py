"""OpenRouter client + config shared by the text workers, delegate loop, and agent.

Reliability lives here: a global concurrency cap (so a wide delegate_run can't
open 100 sockets at once), exponential backoff with jitter on transient errors
(429 / 5xx / timeouts), a per-call timeout, and optional model fallback.
"""

import os
import random
import asyncio

import httpx

import cache

# Worker endpoint. Any OpenAI-compatible base works (OpenRouter, opencode routing,
# Groq, a local server, …). DELEGATE_BASE_URL is the OpenAI base (…/v1); the
# chat-completions path is appended. Key from OPENROUTER_API_KEY or DELEGATE_API_KEY.
BASE_URL = os.environ.get("DELEGATE_BASE_URL", "https://openrouter.ai/api/v1").rstrip(
    "/"
)
OPENROUTER_URL = BASE_URL + "/chat/completions"  # name kept for back-compat
DEFAULT_MODEL = os.environ.get("ASK_MODEL_DEFAULT", "openai/gpt-4o-mini")
API_KEY = os.environ.get("OPENROUTER_API_KEY", "") or os.environ.get(
    "DELEGATE_API_KEY", ""
)
MAX_FILE = 100_000

# Reliability knobs (env-overridable).
MAX_CONCURRENCY = int(os.environ.get("DELEGATE_MAX_CONCURRENCY", "8"))
CALL_TIMEOUT = float(os.environ.get("DELEGATE_TIMEOUT", "180"))
MAX_RETRIES = int(os.environ.get("DELEGATE_RETRIES", "4"))
RETRY_BASE_DELAY = float(os.environ.get("DELEGATE_RETRY_BASE", "1.0"))
RETRY_MAX_DELAY = float(os.environ.get("DELEGATE_RETRY_MAX", "30.0"))
RETRY_STATUS = {429, 500, 502, 503, 504}

# Per-model context windows (tokens). Compaction fires at COMPACT_RATIO of the
# actual window for the model in use — not a fixed number — so a 1M-window model
# isn't trimmed at the same point as a 32k one.
CONTEXT_WINDOWS = {
    "openai/gpt-4o-mini": 128_000,
    "google/gemini-2.0-flash-001": 1_000_000,
    "deepseek/deepseek-chat": 64_000,
    "qwen/qwen-2.5-72b-instruct": 32_000,
    "meta-llama/llama-3.3-70b-instruct": 128_000,
}
DEFAULT_CONTEXT = int(os.environ.get("DELEGATE_DEFAULT_CONTEXT", "32000"))
COMPACT_RATIO = float(os.environ.get("DELEGATE_COMPACT_RATIO", "0.75"))
# Optional hard override; 0 = auto-derive from the model's window.
CONTEXT_OVERRIDE = int(os.environ.get("DELEGATE_CONTEXT_BUDGET", "0"))


def context_budget(model: str) -> int:
    """Token budget that triggers compaction for `model`: COMPACT_RATIO of its
    real context window, unless DELEGATE_CONTEXT_BUDGET overrides it."""
    if CONTEXT_OVERRIDE > 0:
        return CONTEXT_OVERRIDE
    return int(CONTEXT_WINDOWS.get(model, DEFAULT_CONTEXT) * COMPACT_RATIO)


# Caps total concurrent upstream calls across every tool on the server's loop.
SEM = asyncio.Semaphore(MAX_CONCURRENCY)

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "HTTP-Referer": "https://claude.ai/code",
    "X-Title": "delegate-mcp",
    "Content-Type": "application/json",
}


async def chat(client, body, timeout=None):
    """One raw POST to OpenRouter; raises on HTTP error."""
    r = await client.post(
        OPENROUTER_URL, json=body, headers=HEADERS, timeout=timeout or CALL_TIMEOUT
    )
    r.raise_for_status()
    return r.json()


def _retry_after(exc, fallback):
    """Honor a numeric Retry-After header if the server sent one."""
    try:
        ra = exc.response.headers.get("retry-after")
        if ra and ra.strip().isdigit():
            return float(ra)
    except Exception:  # noqa: BLE001
        pass
    return fallback


async def chat_resilient(
    client,
    body,
    timeout=None,
    max_retries=None,
    base_delay=None,
    max_delay=None,
    on_retry=None,
):
    """`chat` with exponential backoff + jitter on transient failures.

    Retries on RETRY_STATUS codes and timeout/transport errors; re-raises
    immediately on non-transient HTTP errors (e.g. 400/401/404) and after the
    retry budget is spent. `on_retry(attempt, wait, err)` is called before each
    sleep (used for logging/tests).
    """
    max_retries = MAX_RETRIES if max_retries is None else max_retries
    base_delay = RETRY_BASE_DELAY if base_delay is None else base_delay
    max_delay = RETRY_MAX_DELAY if max_delay is None else max_delay
    delay = base_delay
    for attempt in range(max_retries + 1):
        try:
            return await chat(client, body, timeout)
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in RETRY_STATUS or attempt == max_retries:
                raise
            wait = _retry_after(e, delay)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            if attempt == max_retries:
                raise
            wait = delay
        jitter = random.uniform(0, base_delay) if base_delay else 0.0
        wait = wait + jitter
        if on_retry:
            on_retry(attempt, wait)
        await asyncio.sleep(wait)
        delay = min(delay * 2, max_delay)


async def call_model(
    client, prompt, model, system="", temperature=0.0, max_tokens=0, fallback=""
):
    """One stateless completion, capped + retried, with optional model fallback.

    Returns {text, model, usage} or {error}.
    """
    if not API_KEY:
        return {"error": "OPENROUTER_API_KEY is not set in this server's env."}
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    primary = model or DEFAULT_MODEL

    # Deterministic, identical request? Return the stored answer for $0, instantly.
    # Keyed on the requested primary model (not whichever fallback answered) so the
    # lookup is stable across runs. Misses and temp>0 calls fall through to the API.
    hit = cache.get(primary, system, prompt, temperature, max_tokens)
    if hit is not None:
        return hit

    candidates = [primary]
    if fallback and fallback not in candidates:
        candidates.append(fallback)

    last_err = "unknown error"
    for m in candidates:
        body = {"model": m, "messages": messages, "temperature": temperature}
        if max_tokens and max_tokens > 0:
            body["max_tokens"] = max_tokens
        try:
            async with SEM:
                data = await chat_resilient(client, body)
            result = {
                "text": data["choices"][0]["message"]["content"],
                "model": m,
                "usage": data.get("usage", {}),
            }
            cache.put(primary, system, prompt, temperature, max_tokens, result)
            return result
        except httpx.HTTPStatusError as e:
            last_err = f"HTTP {e.response.status_code}: {e.response.text[:300]}"
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
    return {"error": last_err}
