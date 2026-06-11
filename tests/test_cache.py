"""Tests for the on-disk worker result cache."""

import sys
import asyncio
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import cache  # noqa: E402
import workers  # noqa: E402
from test_reliability import FakeClient, FakeResp, OK  # noqa: E402


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(cache, "ENABLED", True)
    return tmp_path


def run(coro):
    return asyncio.run(coro)


# ------------------------- unit: put/get -------------------------


def test_roundtrip_deterministic(tmp_cache):
    cache.put("m", "sys", "prompt", 0.0, 0, {"text": "answer", "model": "m"})
    hit = cache.get("m", "sys", "prompt", 0.0, 0)
    assert hit["text"] == "answer"
    assert hit["cached"] is True
    assert hit["usage"] == {}  # free — cost accounting charges $0


def test_miss_returns_none(tmp_cache):
    assert cache.get("m", "", "never-stored", 0.0, 0) is None


def test_key_is_request_specific(tmp_cache):
    cache.put("m", "", "prompt-a", 0.0, 0, {"text": "A", "model": "m"})
    assert cache.get("m", "", "prompt-b", 0.0, 0) is None  # different prompt
    assert cache.get("m2", "", "prompt-a", 0.0, 0) is None  # different model
    assert cache.get("m", "sys", "prompt-a", 0.0, 0) is None  # different system


def test_temp_gt_zero_not_cached(tmp_cache):
    cache.put("m", "", "p", 0.7, 0, {"text": "x", "model": "m"})
    assert cache.get("m", "", "p", 0.7, 0) is None


def test_errors_not_cached(tmp_cache):
    cache.put("m", "", "p", 0.0, 0, {"error": "boom"})
    cache.put("m", "", "p", 0.0, 0, {"text": ""})  # empty text
    assert cache.get("m", "", "p", 0.0, 0) is None


def test_disabled_disables_both(tmp_cache, monkeypatch):
    monkeypatch.setattr(cache, "ENABLED", False)
    cache.put("m", "", "p", 0.0, 0, {"text": "x", "model": "m"})
    assert cache.get("m", "", "p", 0.0, 0) is None


def test_stats_and_clear(tmp_cache):
    cache.put("m", "", "p1", 0.0, 0, {"text": "1", "model": "m"})
    cache.put("m", "", "p2", 0.0, 0, {"text": "2", "model": "m"})
    s = cache.stats()
    assert s["entries"] == 2 and s["bytes"] > 0
    assert cache.clear()["removed"] == 2
    assert cache.stats()["entries"] == 0


# ------------------------- integration: call_model -------------------------


def test_call_model_second_call_is_cache_hit(tmp_cache, monkeypatch):
    monkeypatch.setattr(workers, "API_KEY", "test")
    c = FakeClient([FakeResp(200, OK)])  # only ONE response scripted
    r1 = run(workers.call_model(c, "p", "m"))
    assert r1["text"] == "hi" and not r1.get("cached")
    r2 = run(workers.call_model(c, "p", "m"))  # would IndexError if it hit the API
    assert r2["text"] == "hi" and r2["cached"] is True
    assert c.calls == 1  # network touched exactly once


def test_call_model_temp_bypasses_cache(tmp_cache, monkeypatch):
    monkeypatch.setattr(workers, "API_KEY", "test")
    c = FakeClient([FakeResp(200, OK), FakeResp(200, OK)])
    run(workers.call_model(c, "p", "m", temperature=0.5))
    run(workers.call_model(c, "p", "m", temperature=0.5))
    assert c.calls == 2  # both hit the API; nothing cached
