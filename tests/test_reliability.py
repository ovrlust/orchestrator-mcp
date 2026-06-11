"""Tests for retry/backoff + fallback, driven by a fake async HTTP client."""

import sys
import asyncio
import pathlib

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import workers  # noqa: E402

OK = {"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 1}}


class FakeResp:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = ""
        self.request = httpx.Request("POST", workers.OPENROUTER_URL)

    def raise_for_status(self):
        if self.status_code >= 400:
            resp = httpx.Response(
                self.status_code, request=self.request, headers=self.headers
            )
            raise httpx.HTTPStatusError("err", request=self.request, response=resp)

    def json(self):
        return self._payload


class FakeClient:
    """Replays a scripted list of FakeResp / Exception, counting calls."""

    def __init__(self, behaviors):
        self.behaviors = list(behaviors)
        self.calls = 0

    async def post(self, *a, **k):
        b = self.behaviors[self.calls]
        self.calls += 1
        if isinstance(b, Exception):
            raise b
        return b


def run(coro):
    return asyncio.run(coro)


# ------------------------- chat_resilient -------------------------


def test_retries_then_succeeds():
    c = FakeClient([FakeResp(503), FakeResp(503), FakeResp(200, OK)])
    data = run(workers.chat_resilient(c, {}, base_delay=0))
    assert data == OK
    assert c.calls == 3


def test_non_retryable_raises_immediately():
    c = FakeClient([FakeResp(400)])
    try:
        run(workers.chat_resilient(c, {}, base_delay=0))
        assert False
    except httpx.HTTPStatusError:
        pass
    assert c.calls == 1


def test_exhausts_retry_budget():
    c = FakeClient([FakeResp(503), FakeResp(503), FakeResp(503)])
    try:
        run(workers.chat_resilient(c, {}, max_retries=2, base_delay=0))
        assert False
    except httpx.HTTPStatusError:
        pass
    assert c.calls == 3  # 1 try + 2 retries


def test_timeout_is_retried():
    c = FakeClient([httpx.ReadTimeout("slow"), FakeResp(200, OK)])
    data = run(workers.chat_resilient(c, {}, base_delay=0))
    assert data == OK
    assert c.calls == 2


def test_on_retry_callback_fires():
    seen = []
    c = FakeClient([FakeResp(429), FakeResp(200, OK)])
    run(workers.chat_resilient(c, {}, base_delay=0, on_retry=lambda *a: seen.append(a)))
    assert len(seen) == 1


# ------------------------- call_model fallback -------------------------


def test_fallback_model_used_after_primary_exhausts(monkeypatch):
    monkeypatch.setattr(workers, "API_KEY", "test")
    monkeypatch.setattr(workers, "MAX_RETRIES", 1)
    monkeypatch.setattr(workers, "RETRY_BASE_DELAY", 0.0)
    c = FakeClient([FakeResp(500), FakeResp(500), FakeResp(200, OK)])
    r = run(workers.call_model(c, "p", "primary", fallback="backup"))
    assert r["model"] == "backup"
    assert r["text"] == "hi"
    assert c.calls == 3  # primary: 2 (1+1 retry) exhausts, backup: 1 ok


def test_no_api_key_returns_error():
    # API_KEY is "" in the test env by default
    r = run(workers.call_model(FakeClient([]), "p", "m"))
    assert "error" in r


# ------------------------- context budget (auto) -------------------------


def test_context_budget_scales_with_window(monkeypatch):
    monkeypatch.setattr(workers, "CONTEXT_OVERRIDE", 0)
    monkeypatch.setattr(workers, "COMPACT_RATIO", 0.75)
    assert workers.context_budget("openai/gpt-4o-mini") == int(128_000 * 0.75)
    assert workers.context_budget("qwen/qwen-2.5-72b-instruct") == int(32_000 * 0.75)


def test_context_budget_unknown_model_uses_default(monkeypatch):
    monkeypatch.setattr(workers, "CONTEXT_OVERRIDE", 0)
    monkeypatch.setattr(workers, "COMPACT_RATIO", 0.75)
    monkeypatch.setattr(workers, "DEFAULT_CONTEXT", 32_000)
    assert workers.context_budget("who/knows") == int(32_000 * 0.75)


def test_context_budget_override_wins(monkeypatch):
    monkeypatch.setattr(workers, "CONTEXT_OVERRIDE", 5000)
    assert workers.context_budget("openai/gpt-4o-mini") == 5000
