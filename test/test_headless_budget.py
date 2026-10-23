"""Tests for the headless lane's budgets: render-lock wait and result cache.

Everything here is offline: the browser, the resolver and the cache are all
fakes, so no Chromium is launched and no network hop is made.
"""

import json
import time

import pytest

from app.services import headless_browser as hb
from app.services import result_cache as rc
from app.services.fetch_backend import (
    GoogleHeadlessBackend,
    _bind_per_hop,
)
from app.utils import json_api

QUERY_URL = GoogleHeadlessBackend.search_url + "raspberry+pi+5"

# Three result blocks, each behind a /goto token.
SERP = """
<html><body><div id="main">
  <div data-hveid="CAEQAQ"><a href="/goto?url=ONE"><h3>One</h3></a>
    <div class="VwiC3b">First snippet.</div></div>
  <div data-hveid="CAEQAg"><a href="/goto?url=TWO"><h3>Two</h3></a>
    <div class="VwiC3b">Second snippet.</div></div>
  <div data-hveid="CAEQAw"><a href="/goto?url=THREE"><h3>Three</h3></a>
    <div class="VwiC3b">Third snippet.</div></div>
</div></body></html>
"""

RESOLVED = {
    "/goto?url=ONE": "https://example.com/one",
    "/goto?url=TWO": "https://example.com/two",
    "/goto?url=THREE": "https://example.com/three",
}


def _resolver(href):
    return RESOLVED.get(href)


class FakeBrowser:
    """Stands in for HeadlessBrowser: counts renders, optionally raises."""

    def __init__(self, html=SERP, exc=None):
        self.html = html
        self.exc = exc
        self.renders = 0

    def render(self, url, timeout=None, lock_timeout=None):
        self.renders += 1
        if self.exc is not None:
            raise self.exc
        return self.html


@pytest.fixture(autouse=True)
def cache(monkeypatch):
    """A small, enabled cache, and no network in the resolver."""
    instance = rc.ResultCache(ttl=60, max_entries=8)
    rc.reset_result_cache(instance)
    monkeypatch.setattr(hb, "resolve_result_url", _resolver)
    yield instance
    rc.reset_result_cache(None)


def _use_browser(monkeypatch, browser):
    monkeypatch.setattr(hb, "get_browser", lambda: browser)
    return browser


# -- GAP 1: the result cache ------------------------------------------------


def test_cache_hit_skips_the_render(monkeypatch):
    browser = _use_browser(monkeypatch, FakeBrowser())
    backend = GoogleHeadlessBackend()

    first = backend.fetch(None, QUERY_URL)
    assert first.status_code == 200
    assert browser.renders == 1
    assert first.text.count('class="ZINbbc"') == 3

    started = time.monotonic()
    second = backend.fetch(None, QUERY_URL)
    elapsed = time.monotonic() - started

    # Served from the cache: no second render, and identical results.
    assert browser.renders == 1
    assert second.status_code == 200
    assert second.text == first.text
    assert elapsed < 0.5


def test_distinct_queries_are_cached_separately(monkeypatch):
    browser = _use_browser(monkeypatch, FakeBrowser())
    backend = GoogleHeadlessBackend()
    backend.fetch(None, QUERY_URL)
    backend.fetch(None, GoogleHeadlessBackend.search_url + "another+query")
    assert browser.renders == 2


def test_ttl_expiry_renders_again(monkeypatch):
    clock_now = [1000.0]
    instance = rc.ResultCache(ttl=10, max_entries=8, clock=lambda: clock_now[0])
    rc.reset_result_cache(instance)
    browser = _use_browser(monkeypatch, FakeBrowser())
    backend = GoogleHeadlessBackend()

    backend.fetch(None, QUERY_URL)
    assert browser.renders == 1
    clock_now[0] += 5
    backend.fetch(None, QUERY_URL)
    assert browser.renders == 1          # still fresh
    clock_now[0] += 6                    # past the 10 s TTL
    backend.fetch(None, QUERY_URL)
    assert browser.renders == 2          # re-rendered


def test_cache_disabled_always_renders(monkeypatch):
    rc.reset_result_cache(rc.ResultCache(ttl=0, max_entries=8))
    browser = _use_browser(monkeypatch, FakeBrowser())
    backend = GoogleHeadlessBackend()
    backend.fetch(None, QUERY_URL)
    backend.fetch(None, QUERY_URL)
    assert browser.renders == 2


def test_challenge_is_not_cached(monkeypatch):
    browser = _use_browser(
        monkeypatch, FakeBrowser(exc=hb.HeadlessChallenge("sorry")))
    backend = GoogleHeadlessBackend()

    first = backend.fetch(None, QUERY_URL)
    assert first.status_code == 429
    second = backend.fetch(None, QUERY_URL)
    assert second.status_code == 429
    # A wall page must never be served later as a normal answer.
    assert browser.renders == 2
    assert len(rc.get_result_cache()) == 0


def test_unavailable_browser_is_not_cached(monkeypatch):
    browser = _use_browser(
        monkeypatch, FakeBrowser(exc=hb.HeadlessUnavailable("no chromium")))
    backend = GoogleHeadlessBackend()
    assert backend.fetch(None, QUERY_URL).status_code == 503
    assert backend.fetch(None, QUERY_URL).status_code == 503
    assert browser.renders == 2
    assert len(rc.get_result_cache()) == 0


def test_empty_result_set_is_not_cached(monkeypatch):
    empty_serp = "<html><body><div id=\"main\"></div></body></html>"
    browser = _use_browser(monkeypatch, FakeBrowser(html=empty_serp))
    backend = GoogleHeadlessBackend()
    page = backend.fetch(None, QUERY_URL)
    assert page.status_code == 200
    assert page.text.count('class="ZINbbc"') == 0
    backend.fetch(None, QUERY_URL)
    assert browser.renders == 2
    assert len(rc.get_result_cache()) == 0


def test_cache_hit_does_not_touch_the_resolver(monkeypatch):
    browser = _use_browser(monkeypatch, FakeBrowser())
    calls = []

    def counting_resolver(href):
        calls.append(href)
        return RESOLVED.get(href)

    monkeypatch.setattr(hb, "resolve_result_url", counting_resolver)
    backend = GoogleHeadlessBackend()

    backend.fetch(None, QUERY_URL)
    assert len(calls) == 3          # one hop per result on the cold path
    backend.fetch(None, QUERY_URL)
    # A hit skips the /goto hops entirely: the stored DOM already carries the
    # resolved absolute URLs.
    assert len(calls) == 3
    assert browser.renders == 1


# -- GAP 2: bounded wait for the render lock --------------------------------


def _browser_with_fake_render(html="<html>rendered</html>"):
    browser = hb.HeadlessBrowser(chromium="/bin/true")
    browser._ensure_started = lambda: None
    browser._render_locked = lambda url, timeout: html
    return browser


def test_lock_acquisition_times_out_with_a_clear_error():
    browser = _browser_with_fake_render()
    browser._lock.acquire()
    try:
        started = time.monotonic()
        with pytest.raises(hb.HeadlessBusy) as excinfo:
            browser.render("https://www.google.com/search?q=x",
                           lock_timeout=0.05)
        elapsed = time.monotonic() - started
    finally:
        browser._lock.release()
    assert "busy" in str(excinfo.value)
    # Bounded: it gave up at the budget instead of blocking for ever.
    assert elapsed < 1.0


def test_a_successful_render_releases_the_lock():
    browser = _browser_with_fake_render()
    assert browser.render("https://www.google.com/search?q=x",
                          lock_timeout=0.05) == "<html>rendered</html>"
    # The lock is free again, so an immediate second call succeeds.
    assert browser.render("https://www.google.com/search?q=x",
                          lock_timeout=0.05) == "<html>rendered</html>"


def test_lock_timeout_surfaces_as_a_documented_timeout(monkeypatch):
    browser = _use_browser(
        monkeypatch, FakeBrowser(exc=hb.HeadlessBusy("busy")))
    page = GoogleHeadlessBackend().fetch(None, QUERY_URL)
    assert page.status_code == 504
    # A timeout is not "no results", and it is never cached.
    assert json_api.classify_empty(page.status_code, page.text) == \
        json_api.ERROR_TIMEOUT
    assert len(rc.get_result_cache()) == 0
    assert browser.renders == 1


def test_lock_timeout_env_is_read(monkeypatch):
    monkeypatch.setenv("WHOOGLE_HEADLESS_LOCK_TIMEOUT", "12.5")
    assert hb.resolve_lock_timeout() == 12.5
    monkeypatch.setenv("WHOOGLE_HEADLESS_LOCK_TIMEOUT", "nonsense")
    assert hb.resolve_lock_timeout() == hb.DEFAULT_LOCK_TIMEOUT
    monkeypatch.setenv("WHOOGLE_HEADLESS_LOCK_TIMEOUT", "0")
    assert hb.resolve_lock_timeout() == hb.DEFAULT_LOCK_TIMEOUT


# -- GAP 3: the /goto resolution budget -------------------------------------


def test_resolve_budget_drops_the_remaining_results(monkeypatch):
    def slow_resolver(href):
        time.sleep(0.15)
        return RESOLVED.get(href)

    backend = GoogleHeadlessBackend()
    started = time.monotonic()
    html, stats = backend._normalize(SERP, resolve=slow_resolver, budget=0.05)
    elapsed = time.monotonic() - started

    # The first hop fits in the budget; the rest are dropped, not waited on.
    assert stats["candidates"] == 3
    assert stats["resolved"] == 1
    assert stats["dropped"] == 3 - stats["resolved"]
    assert stats["budget_exhausted"] is True
    assert html.count('class="ZINbbc"') == 1
    assert elapsed < 1.0


def test_partial_resolution_is_served_but_not_cached(monkeypatch):
    def half_resolver(href):
        # Resolve only ONE, and burn the budget doing it.
        time.sleep(0.15)
        return RESOLVED.get(href) if href.endswith("ONE") else None

    browser = _use_browser(monkeypatch, FakeBrowser())
    monkeypatch.setattr(hb, "resolve_result_url", half_resolver)
    monkeypatch.setenv("WHOOGLE_HEADLESS_RESOLVE_TIMEOUT", "0.05")

    page = GoogleHeadlessBackend().fetch(None, QUERY_URL)
    # A partial SERP is still a truthful answer (fewer results, no error)...
    assert page.status_code == 200
    assert page.text.count('class="ZINbbc"') == 1
    # ...but it is not cached, so the next request can try the rest again.
    assert len(rc.get_result_cache()) == 0
    assert browser.renders == 1


def test_no_resolvable_result_is_a_timeout_not_an_empty_answer(monkeypatch):
    _use_browser(monkeypatch, FakeBrowser())
    monkeypatch.setattr(hb, "resolve_result_url", lambda href: None)
    page = GoogleHeadlessBackend().fetch(None, QUERY_URL)
    # Every candidate existed but none resolved: "no results" would be a lie.
    assert page.status_code == 504
    assert json_api.classify_empty(page.status_code, page.text) == \
        json_api.ERROR_TIMEOUT
    assert len(rc.get_result_cache()) == 0


def test_zero_candidate_serp_is_an_honest_empty_success(monkeypatch):
    _use_browser(monkeypatch, FakeBrowser(
        html='<html><body><div id="main"></div></body></html>'))
    page = GoogleHeadlessBackend().fetch(None, QUERY_URL)
    assert page.status_code == 200
    assert json_api.classify_empty(page.status_code, page.text) is None


def test_resolve_hop_timeout_is_capped_by_the_budget():
    assert hb.resolve_hop_timeout(20) == hb.DEFAULT_RESOLVE_HOP_TIMEOUT
    # A budget smaller than the default hop cap wins, so a hop can never
    # outlive the phase budget.
    assert hb.resolve_hop_timeout(0.5) == 0.5


def test_resolve_timeout_env_and_defaults(monkeypatch):
    monkeypatch.delenv("WHOOGLE_HEADLESS_RESOLVE_TIMEOUT", raising=False)
    assert hb.resolve_resolve_timeout() == hb.DEFAULT_RESOLVE_TIMEOUT
    monkeypatch.setenv("WHOOGLE_HEADLESS_RESOLVE_TIMEOUT", "7")
    assert hb.resolve_resolve_timeout() == 7
    assert hb.resolve_hop_timeout() == min(hb.DEFAULT_RESOLVE_HOP_TIMEOUT, 7)


def test_bind_per_hop_leaves_a_one_argument_resolver_alone():
    assert _bind_per_hop(_resolver, 5.0) is _resolver

    seen = []

    def two_arg_resolver(href, timeout=15.0):
        seen.append(timeout)
        return "https://example.com/x"

    bound = _bind_per_hop(two_arg_resolver, 2.5)
    assert bound("/goto?url=X") == "https://example.com/x"
    # The per-hop timeout from the budget is what actually gets used.
    assert seen == [2.5]


def test_classify_empty_maps_504_to_timeout():
    assert json_api.classify_empty(504, '<div id="main"></div>') == \
        json_api.ERROR_TIMEOUT


# -- the contract, end to end through the route -----------------------------


def test_route_reports_a_browser_timeout_as_timeout(client, monkeypatch):
    from app.models.endpoint import Endpoint
    from app.utils import search as search_mod

    def fake_new_query(self):
        self.query = 'test'
        return self.query

    def fake_generate(self):
        self.upstream_status = 504
        self.upstream_raw = '<div id="main"></div>'
        return '<div id="main"></div>'

    monkeypatch.setattr(search_mod.Search, 'new_search_query', fake_new_query)
    monkeypatch.setattr(search_mod.Search, 'generate_response', fake_generate)

    rv = client.get(f'/{Endpoint.search}?q=test&format=json')
    assert rv._status_code == 504
    data = json.loads(rv.data)
    assert data['error_type'] == 'timeout'
    assert data['count'] == 0
    assert data['results'] == []
