"""Tests for the google-headless backend and its headless driver helpers."""

import urllib.error

from app.services import headless_browser as hb
from app.services.fetch_backend import (
    GoogleHeadlessBackend,
    _extract_serp_items,
    get_backend,
)

# A trimmed but structurally faithful rendered Google SERP: the title is an
# `a` wrapping an `h3`, the result container is a `div[data-hveid]`, and the
# snippet is a `.VwiC3b` element. The anchor with no h3 and the duplicate are
# decoys that must be ignored/deduped.
GOOGLE_SERP = """
<html><body>
<div id="main">
  <div data-hveid="CAEQAQ">
    <div class="something">
      <a href="/goto?url=TOKEN_ONE"><h3>Raspberry Pi 5 Review</h3></a>
      <div class="VwiC3b">The &amp; latest Raspberry Pi is much faster.</div>
    </div>
  </div>
  <div data-hveid="CAEQAg">
    <div class="something">
      <a href="/goto?url=TOKEN_TWO"><h3>Second result title</h3></a>
      <div class="VwiC3b">A different snippet.</div>
    </div>
  </div>
  <a href="https://example.com/plain">An anchor without an h3</a>
  <div data-hveid="CAEQAw">
    <a href="/goto?url=TOKEN_ONE"><h3>Raspberry Pi 5 Review</h3></a>
  </div>
  <div data-hveid="CAEQBA">
    <a href="/goto?url=TOKEN_DEAD"><h3>Unresolvable result</h3></a>
  </div>
</div>
</body></html>
"""

_RESOLVED = {
    "/goto?url=TOKEN_ONE":
        "https://www.tomshardware.com/reviews/raspberry-pi-5",
    "/goto?url=TOKEN_TWO":
        "https://www.pcmag.com/reviews/raspberry-pi-5",
    "/goto?url=TOKEN_DEAD": None,
}


def _fake_resolver(href):
    return _RESOLVED.get(href)


def test_get_backend_selects_google_headless(monkeypatch):
    monkeypatch.setenv("WHOOGLE_FETCH_BACKEND", "google-headless")
    backend = get_backend()
    assert isinstance(backend, GoogleHeadlessBackend)
    assert backend.search_url == "https://www.google.com/search?q="
    assert backend.image_search_url == \
        "https://www.google.com/search?udm=2&q="


def test_extract_serp_items_ignores_h3less_and_reads_snippet():
    items = _extract_serp_items(GOOGLE_SERP)
    # The h3-less anchor is ignored; the 4 h3 anchors (incl. the TOKEN_ONE
    # duplicate) are returned - dedup happens later, in parse().
    assert len(items) == 4
    assert items[0]["title"] == "Raspberry Pi 5 Review"
    assert items[0]["href"] == "/goto?url=TOKEN_ONE"
    assert "latest Raspberry Pi" in items[0]["snippet"]


def test_parse_builds_whoogle_shaped_dom_with_resolved_urls():
    html = GoogleHeadlessBackend().parse(GOOGLE_SERP, resolve=_fake_resolver)

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    assert soup.find("div", {"id": "main"}) is not None
    result_divs = soup.find_all("div", class_="ZINbbc")
    # TOKEN_ONE deduped, TOKEN_DEAD dropped, h3-less anchor ignored -> 2.
    assert len(result_divs) == 2

    first = result_divs[0]
    a = first.find("h3").find("a")
    assert a["href"] == \
        "https://www.tomshardware.com/reviews/raspberry-pi-5"
    assert a.get_text(strip=True) == "Raspberry Pi 5 Review"
    snippet = first.find("span", class_="VwiC3b")
    assert snippet is not None and "Raspberry Pi" in snippet.get_text()

    assert result_divs[1].find("h3").find("a")["href"] == \
        "https://www.pcmag.com/reviews/raspberry-pi-5"


def test_parse_empty_input_is_valid():
    html = GoogleHeadlessBackend().parse("", resolve=_fake_resolver)
    assert html == '<div id="main"></div>'


def test_resolve_result_url_pure_paths():
    assert hb.resolve_result_url("") is None
    # Already-absolute third-party URL passes through untouched (no network).
    assert hb.resolve_result_url("https://example.com/page") == \
        "https://example.com/page"
    # Legacy classic redirect form is unwrapped without a network hop.
    assert hb.resolve_result_url(
        "/url?q=https%3A%2F%2Fx.example%2Fa&sa=U") == "https://x.example/a"


def test_resolve_result_url_follows_goto_redirect(monkeypatch):
    class _FakeOpener:
        def open(self, req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 302, "Found",
                {"Location": "https://real.example/target"}, None)

    monkeypatch.setattr(hb.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener())
    assert hb.resolve_result_url("/goto?url=TOKEN") == \
        "https://real.example/target"


class _FakeBrowser:
    def __init__(self, html=None, exc=None):
        self.html = html
        self.exc = exc

    def render(self, url, timeout=None):
        if self.exc:
            raise self.exc
        return self.html


def _fetch_with(fake):
    return GoogleHeadlessBackend().fetch(
        None, GoogleHeadlessBackend.search_url + "q")


def test_fetch_returns_empty_result_set_on_challenge(monkeypatch):
    monkeypatch.setattr(hb, "get_browser", lambda: _FakeBrowser(
        exc=hb.HeadlessChallenge("sorry")))
    page = _fetch_with(None)
    assert page.status_code == 429
    assert page.text == '<div id="main"></div>'


def test_fetch_returns_empty_result_set_when_unavailable(monkeypatch):
    monkeypatch.setattr(hb, "get_browser", lambda: _FakeBrowser(
        exc=hb.HeadlessUnavailable("no chromium")))
    page = _fetch_with(None)
    assert page.status_code == 503
    assert page.text == '<div id="main"></div>'


def test_fetch_normalizes_a_rendered_serp(monkeypatch):
    monkeypatch.setattr(
        hb, "get_browser", lambda: _FakeBrowser(html=GOOGLE_SERP))
    monkeypatch.setattr(hb, "resolve_result_url", _fake_resolver)
    page = GoogleHeadlessBackend().fetch(
        None, GoogleHeadlessBackend.search_url + "raspberry+pi+5")
    assert page.status_code == 200
    assert page.text.count('class="ZINbbc"') == 2


def test_looks_like_challenge():
    # "/sorry/" appears in a normal SERP's own JS -> NOT a challenge.
    assert hb.looks_like_challenge(
        '<title>x - Google Search</title>/sorry/index', 8) is False
    # A real results page is never a challenge.
    assert hb.looks_like_challenge('<title>x</title>', 3) is False
    # No anchors + challenge title/text -> challenge.
    assert hb.looks_like_challenge('<title>Sorry...</title>', 0) is True
    assert hb.looks_like_challenge(
        '<title>x</title>unusual traffic', 0) is True
    # Empty page: not a challenge (a genuine zero-result query).
    assert hb.looks_like_challenge('', 0) is False
    assert hb.looks_like_challenge('<html>nothing</html>', 0) is False


def test_find_chromium_honors_env(monkeypatch):
    monkeypatch.setenv("WHOOGLE_HEADLESS_CHROMIUM", "/usr/bin/chromium")
    assert hb._find_chromium() == "/usr/bin/chromium"


def test_shutdown_without_start_is_safe():
    # A browser that was never started must shut down cleanly and remove
    # nothing it did not create.
    browser = hb.HeadlessBrowser(profile_dir="")
    browser.shutdown()
    browser.shutdown()  # idempotent
