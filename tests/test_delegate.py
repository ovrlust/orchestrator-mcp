"""Tests for the delegate orchestrator pre-flight (network-free)."""

import sys
import asyncio
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import delegate  # noqa: E402


def test_preflight_ok():
    assert delegate.preflight([{"id": "a"}, {"id": "b", "depends_on": ["a"]}]) is None


def test_preflight_empty():
    assert "non-empty" in delegate.preflight([])


def test_preflight_missing_id():
    assert "id" in delegate.preflight([{"prompt": "x"}])


def test_preflight_duplicate_ids():
    err = delegate.preflight([{"id": "a"}, {"id": "a"}])
    assert "duplicate" in err and "a" in err


def test_preflight_self_dependency():
    err = delegate.preflight([{"id": "a", "depends_on": ["a"]}])
    assert "depends on itself" in err


def test_run_delegate_rejects_bad_orders_before_api_key():
    # preflight runs before the API-key check, so a dup-id batch errors clearly
    # even with no key configured (the test env has none).
    r = asyncio.run(delegate.run_delegate([{"id": "x"}, {"id": "x"}], "/tmp"))
    assert "duplicate" in r["error"]
