"""Provider abstraction: one normalized streaming interface over multiple LLMs.

The orchestrator works in ONE internal (OpenAI-ish) message + tool format; each
provider adapts it to/from its wire format and yields a normalized event stream:

  {"type": "text", "text": str}                       streamed token delta
  {"type": "tool_call", "id", "name", "args": dict}   a complete tool request
  {"type": "usage", "prompt_tokens", "completion_tokens"}
  {"type": "done", "stop_reason": str}
  {"type": "error", "error": str}

Internal message format:
  {"role": "system"|"user"|"assistant", "content": str}
  assistant tool calls: {"role":"assistant","content":str|None,"tool_calls":[{"id","name","args":dict}]}
  tool result:          {"role":"tool","tool_call_id":str,"name":str,"content":str}

Tools are the OpenAI function-schema list: [{"type":"function","function":{name,description,parameters}}].
"""

import os
import json

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


def _key(name):
    return os.environ.get(name, "")


def resolve_key(provider: str, override: str = "") -> str:
    """Per-session override key wins; otherwise fall back to the env key."""
    if override:
        return override
    return _key(
        "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENROUTER_API_KEY"
    )


# ------------------------- format adapters (pure, testable) -------------------------


def to_openai(messages: list) -> list:
    """Internal -> OpenAI chat format."""
    out = []
    for m in messages:
        role = m["role"]
        if role == "assistant" and m.get("tool_calls"):
            out.append(
                {
                    "role": "assistant",
                    "content": m.get("content"),
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc.get("args", {})),
                            },
                        }
                        for tc in m["tool_calls"]
                    ],
                }
            )
        elif role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": m.get("tool_call_id", ""),
                    "content": m.get("content", ""),
                }
            )
        else:
            out.append({"role": role, "content": m.get("content", "")})
    return out


def to_anthropic(messages: list):
    """Internal -> (system_str, anthropic_messages). Groups consecutive tool
    results into a single user turn, as the Messages API requires."""
    system, out, pending = [], [], []

    def flush():
        if pending:
            out.append({"role": "user", "content": list(pending)})
            pending.clear()

    for m in messages:
        role = m["role"]
        if role == "system":
            if m.get("content"):
                system.append(m["content"])
            continue
        if role == "tool":
            pending.append(
                {
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id", ""),
                    "content": m.get("content", ""),
                }
            )
            continue
        flush()
        if role == "assistant":
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls") or []:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["name"],
                        "input": tc.get("args", {}),
                    }
                )
            out.append(
                {
                    "role": "assistant",
                    "content": blocks if blocks else (m.get("content") or ""),
                }
            )
        else:
            out.append({"role": "user", "content": m.get("content", "")})
    flush()
    return "\n\n".join(system), out


def tools_to_anthropic(tools: list) -> list:
    return [
        {
            "name": t["function"]["name"],
            "description": t["function"].get("description", ""),
            "input_schema": t["function"].get(
                "parameters", {"type": "object", "properties": {}}
            ),
        }
        for t in (tools or [])
    ]


# ------------------------- streaming (network) -------------------------


async def _openrouter_stream(
    messages, tools, model, max_tokens, temperature, api_key=""
):
    key = resolve_key("openrouter", api_key)
    if not key:
        yield {"type": "error", "error": "OPENROUTER_API_KEY not set"}
        return
    headers = {
        "Authorization": f"Bearer {key}",
        "HTTP-Referer": "https://claude.ai/code",
        "X-Title": "delegate-harness",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "messages": to_openai(messages),
        "stream": True,
        "temperature": temperature,
        "stream_options": {"include_usage": True},
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if max_tokens:
        body["max_tokens"] = max_tokens
    acc, stop = {}, None
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream(
            "POST", OPENROUTER_URL, json=body, headers=headers
        ) as r:
            if r.status_code >= 400:
                yield {
                    "type": "error",
                    "error": f"HTTP {r.status_code}: {(await r.aread())[:300]}",
                }
                return
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except Exception:  # noqa: BLE001
                    continue
                if chunk.get("usage"):
                    u = chunk["usage"]
                    yield {
                        "type": "usage",
                        "prompt_tokens": u.get("prompt_tokens", 0),
                        "completion_tokens": u.get("completion_tokens", 0),
                    }
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                ch = choices[0]
                delta = ch.get("delta") or {}
                if delta.get("content"):
                    yield {"type": "text", "text": delta["content"]}
                for tc in delta.get("tool_calls") or []:
                    a = acc.setdefault(
                        tc.get("index", 0), {"id": "", "name": "", "args": ""}
                    )
                    if tc.get("id"):
                        a["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        a["name"] = fn["name"]
                    if fn.get("arguments"):
                        a["args"] += fn["arguments"]
                if ch.get("finish_reason"):
                    stop = ch["finish_reason"]
    for idx in sorted(acc):
        a = acc[idx]
        try:
            args = json.loads(a["args"] or "{}")
        except Exception:  # noqa: BLE001
            args = {"_raw": a["args"]}
        yield {
            "type": "tool_call",
            "id": a["id"] or f"call_{idx}",
            "name": a["name"],
            "args": args,
        }
    yield {"type": "done", "stop_reason": stop or "stop"}


async def _anthropic_stream(
    messages, tools, model, max_tokens, temperature, api_key=""
):
    key = resolve_key("anthropic", api_key)
    if not key:
        yield {"type": "error", "error": "ANTHROPIC_API_KEY not set"}
        return
    headers = {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
        "Content-Type": "application/json",
    }
    system, amsgs = to_anthropic(messages)
    body = {
        "model": model,
        "messages": amsgs,
        "max_tokens": max_tokens or 4096,
        "temperature": temperature,
        "stream": True,
    }
    if system:
        body["system"] = system
    if tools:
        body["tools"] = tools_to_anthropic(tools)
    blocks, stop, usage = {}, None, {}
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream(
            "POST", ANTHROPIC_URL, json=body, headers=headers
        ) as r:
            if r.status_code >= 400:
                yield {
                    "type": "error",
                    "error": f"HTTP {r.status_code}: {(await r.aread())[:300]}",
                }
                return
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    evt = json.loads(line[5:].strip())
                except Exception:  # noqa: BLE001
                    continue
                t = evt.get("type")
                if t == "message_start":
                    usage["prompt_tokens"] = (
                        evt.get("message", {}).get("usage", {}).get("input_tokens", 0)
                    )
                elif t == "content_block_start":
                    cb = evt.get("content_block", {})
                    if cb.get("type") == "tool_use":
                        blocks[evt["index"]] = {
                            "type": "tool_use",
                            "id": cb["id"],
                            "name": cb["name"],
                            "json": "",
                        }
                    else:
                        blocks[evt["index"]] = {"type": "text"}
                elif t == "content_block_delta":
                    d = evt.get("delta", {})
                    if d.get("type") == "text_delta":
                        yield {"type": "text", "text": d.get("text", "")}
                    elif d.get("type") == "input_json_delta":
                        blocks[evt["index"]]["json"] += d.get("partial_json", "")
                elif t == "message_delta":
                    stop = evt.get("delta", {}).get("stop_reason", stop)
                    usage["completion_tokens"] = evt.get("usage", {}).get(
                        "output_tokens", usage.get("completion_tokens", 0)
                    )
                elif t == "message_stop":
                    break
    for idx in sorted(blocks):
        b = blocks[idx]
        if b.get("type") == "tool_use":
            try:
                args = json.loads(b["json"] or "{}")
            except Exception:  # noqa: BLE001
                args = {"_raw": b["json"]}
            yield {"type": "tool_call", "id": b["id"], "name": b["name"], "args": args}
    if usage:
        yield {
            "type": "usage",
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
        }
    yield {"type": "done", "stop_reason": stop or "end_turn"}


PROVIDERS = {"openrouter": _openrouter_stream, "anthropic": _anthropic_stream}
DEFAULT_PROVIDER = "openrouter"
DEFAULT_MODELS = {
    "openrouter": "anthropic/claude-3.5-sonnet",
    "anthropic": "claude-sonnet-4-6",
}


def stream(
    provider: str, messages, tools, model, max_tokens=0, temperature=0.0, api_key=""
):
    """Return the normalized async event generator for the chosen provider."""
    fn = PROVIDERS.get(provider or DEFAULT_PROVIDER)
    if not fn:

        async def _bad():
            yield {"type": "error", "error": f"unknown provider: {provider}"}

        return _bad()
    return fn(
        messages,
        tools,
        model or DEFAULT_MODELS.get(provider, ""),
        max_tokens,
        temperature,
        api_key,
    )
