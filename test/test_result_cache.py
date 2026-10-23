"""Tests for the headless lane's TTL result cache (app/services/result_cache)."""

import pytest

from app.services import result_cache as rc


class FakeClock:
    """A monotonic clock we can advance by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture(autouse=True)
def clean_singleton():
    """Never let one test's cache leak into another."""
    rc.reset_result_cache(None)
    yield
    rc.reset_result_cache(None)


def test_set_then_get_is_a_hit():
    cache = rc.ResultCache(ttl=60, max_entries=4)
    assert cache.get("https://www.google.com/search?q=x") is None
    cache.set("https://www.google.com/search?q=x", "<div>results</div>")
    assert cache.get("https://www.google.com/search?q=x") == "<div>results</div>"


def test_distinct_keys_do_not_collide():
    cache = rc.ResultCache(ttl=60, max_entries=4)
    cache.set("a", "A")
    cache.set("b", "B")
    assert cache.get("a") == "A"
    assert cache.get("b") == "B"
    assert cache.get("c") is None


def test_entry_expires_after_ttl():
    clock = FakeClock()
    cache = rc.ResultCache(ttl=10, max_entries=4, clock=clock)
    cache.set("a", "A")
    clock.advance(9.9)
    assert cache.get("a") == "A"
    clock.advance(0.2)
    # Expired: a miss, and the dead entry is dropped rather than served.
    assert cache.get("a") is None
    assert len(cache) == 0


def test_ttl_zero_disables_the_cache():
    cache = rc.ResultCache(ttl=0, max_entries=4)
    assert cache.enabled is False
    cache.set("a", "A")
    assert cache.get("a") is None
    assert len(cache) == 0


def test_zero_capacity_disables_the_cache():
    cache = rc.ResultCache(ttl=60, max_entries=0)
    assert cache.enabled is False
    cache.set("a", "A")
    assert cache.get("a") is None


def test_size_cap_evicts_the_oldest_entry_first():
    cache = rc.ResultCache(ttl=60, max_entries=3)
    for key in ("a", "b", "c"):
        cache.set(key, key.upper())
    cache.set("d", "D")
    assert len(cache) == 3
    # 'a' was inserted first, so it is the one evicted.
    assert cache.get("a") is None
    assert [cache.get(k) for k in ("b", "c", "d")] == ["B", "C", "D"]


def test_resetting_a_key_refreshes_it_and_its_ttl():
    clock = FakeClock()
    cache = rc.ResultCache(ttl=10, max_entries=2, clock=clock)
    cache.set("a", "old")
    clock.advance(8)
    cache.set("a", "new")
    clock.advance(8)
    # 16 s after the first write, but only 8 s after the refresh.
    assert cache.get("a") == "new"


def test_none_is_never_cached():
    cache = rc.ResultCache(ttl=60, max_entries=4)
    cache.set("a", None)
    assert cache.get("a") is None
    assert len(cache) == 0


def test_clear_empties_the_cache():
    cache = rc.ResultCache(ttl=60, max_entries=4)
    cache.set("a", "A")
    cache.clear()
    assert cache.get("a") is None


def test_from_env_reads_the_documented_variables(monkeypatch):
    monkeypatch.setenv("WHOOGLE_CACHE_TTL", "120")
    monkeypatch.setenv("WHOOGLE_CACHE_MAX_ENTRIES", "7")
    cache = rc.from_env()
    assert cache.ttl == 120
    assert cache.max_entries == 7
    assert cache.enabled is True


def test_from_env_defaults(monkeypatch):
    monkeypatch.delenv("WHOOGLE_CACHE_TTL", raising=False)
    monkeypatch.delenv("WHOOGLE_CACHE_MAX_ENTRIES", raising=False)
    cache = rc.from_env()
    assert cache.ttl == rc.DEFAULT_TTL
    assert cache.max_entries == rc.DEFAULT_MAX_ENTRIES


def test_from_env_ttl_zero_means_disabled(monkeypatch):
    monkeypatch.setenv("WHOOGLE_CACHE_TTL", "0")
    assert rc.from_env().enabled is False


def test_from_env_ignores_unparseable_values(monkeypatch):
    monkeypatch.setenv("WHOOGLE_CACHE_TTL", "not-a-number")
    monkeypatch.setenv("WHOOGLE_CACHE_MAX_ENTRIES", "")
    cache = rc.from_env()
    assert cache.ttl == rc.DEFAULT_TTL
    assert cache.max_entries == rc.DEFAULT_MAX_ENTRIES


def test_singleton_is_memoised_and_can_be_reset(monkeypatch):
    monkeypatch.setenv("WHOOGLE_CACHE_TTL", "60")
    first = rc.get_result_cache()
    assert rc.get_result_cache() is first
    rc.reset_result_cache(None)
    assert rc.get_result_cache() is not first
