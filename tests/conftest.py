"""Shared test fixtures.

The worker result cache is disabled by default for the whole suite so call_model
tests stay deterministic (a persisted on-disk hit must not short-circuit a test
that asserts on network calls). The cache's own tests re-enable it against a
temp dir via their `tmp_cache` fixture.
"""

import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import cache  # noqa: E402


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    monkeypatch.setattr(cache, "ENABLED", False)
