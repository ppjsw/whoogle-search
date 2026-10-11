"""Tests for the pluggable fetch backends (WHOOGLE_FETCH_BACKEND)."""

from bs4 import BeautifulSoup

from app.services.fetch_backend import (
    DDG_HTML_URL,
    DEFAULT_FETCH_BACKEND,
    DdgHtmlBackend,
    GoogleHeadlessBackend,
    GoogleHtmlBackend,
    _resolve_target,
    get_backend,
)

# A trimmed but structurally faithful DuckDuckGo result page (the real markup
# uses div.result.result__body > h2.result__title > a.result__a and an
# a.result__snippet).
DDG_SAMPLE = """
<html><body>
<div class="results">
  <div class="result results_links results_links_deep web-result">
    <div class="links_main links_deep result__body">
      <h2 class="result__title">
        <a rel="nofollow" class="result__a"
           href="https://www.raspberrypi.com/products/raspberry-pi-5/">Buy a Raspberry Pi 5</a>
      </h2>
      <div class="result__extras">
        <div class="result__extras__url">www.raspberrypi.com/products/raspberry-pi-5/</div>
      </div>
      <a class="result__snippet" href="https://www.raspberrypi.com/products/raspberry-pi-5/">
        The latest release of Raspberry Pi OS &amp; friends.
      </a>
    </div>
  </div>
  <div class="result results_links results_links_deep web-result">
    <div class="links_main links_deep result__body">
      <h2 class="result__title">
        <a rel="nofollow" class="result__a"
           href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Freal%3Fx%3D1&amp;rut=abc">
          Example &quot;redirected&quot; result
        </a>
      </h2>
      <a class="result__snippet">A wrapped link whose real target lives in uddg.</a>
    </div>
  </div>
</div>
</body></html>
"""


def test_get_backend_default_and_selection(monkeypatch):
    monkeypatch.delenv("WHOOGLE_FETCH_BACKEND", raising=False)
    assert isinstance(get_backend(), GoogleHtmlBackend)
    assert get_backend().name == DEFAULT_FETCH_BACKEND == "google-html"

    monkeypatch.setenv("WHOOGLE_FETCH_BACKEND", "ddg-html")
    assert isinstance(get_backend(), DdgHtmlBackend)

    monkeypatch.setenv("WHOOGLE_FETCH_BACKEND", "google-headless")
    assert isinstance(get_backend(), GoogleHeadlessBackend)
    assert get_backend().name == "google-headless"

    # Unknown backends still fall back safely.
    assert isinstance(get_backend("nonsense"), GoogleHtmlBackend)
    # Case/whitespace tolerant.
    assert isinstance(get_backend(" DDG-HTML "), DdgHtmlBackend)


def test_resolve_target_unwraps_uddg():
    wrapped = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Freal%3Fx%3D1&rut=abc"
    assert _resolve_target(wrapped) == "https://example.com/real?x=1"
    assert _resolve_target("https://direct.example/page") == "https://direct.example/page"
    assert _resolve_target("") == ""


def test_parse_produces_whoogle_shaped_dom():
    html = DdgHtmlBackend().parse(DDG_SAMPLE)
    soup = BeautifulSoup(html, "html.parser")

    main = soup.find("div", {"id": "main"})
    assert main is not None

    result_divs = soup.find_all("div", class_="ZINbbc")
    assert len(result_divs) == 2

    # Title link is a real, unwrapped http URL; text round-trips correctly.
    first = result_divs[0]
    a = first.find("h3").find("a")
    assert a["href"] == "https://www.raspberrypi.com/products/raspberry-pi-5/"
    assert a.get_text(strip=True) == "Buy a Raspberry Pi 5"

    snippet = first.find("span", class_="VwiC3b")
    assert snippet is not None
    assert "Raspberry Pi OS" in snippet.get_text()

    # The uddg-wrapped link was resolved to its real target.
    second = result_divs[1].find("h3").find("a")
    assert second["href"] == "https://example.com/real?x=1"


def test_google_backend_parse_is_identity():
    raw = "<html><body><div id='main'>raw google html</div></body></html>"
    assert GoogleHtmlBackend().parse(raw) == raw


class _FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


class _FakeClient:
    """Records the POST and replays a DDG page."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, headers=None, cookies=None, data=None):
        self.calls.append((url, data))
        return self.response


def test_ddg_fetch_posts_raw_query_and_normalizes():
    client = _FakeClient(_FakeResponse(DDG_SAMPLE, 200))
    backend = DdgHtmlBackend()

    # gen_query appends Whoogle args after the (percent-encoded) query.
    page = backend.fetch(
        client,
        DDG_HTML_URL + "raspberry%20pi%205&safe=off&gl=US",
        headers={"User-Agent": "test"},
    )

    assert client.calls == [(DDG_HTML_URL, {"q": "raspberry pi 5"})]
    assert page.status_code == 200
    assert "<div id=\"main\">" in page.text
    assert page.text.count('class="ZINbbc"') == 2


def test_ddg_fetch_handles_empty_response():
    client = _FakeClient(_FakeResponse("", 202))
    backend = DdgHtmlBackend()
    backend.RETRY_DELAY = 0  # no sleep in tests
    page = backend.fetch(client, DDG_HTML_URL + "x", headers={})
    # An anomaly/challenge page yields an empty (but valid) result set, not a
    # crash - and is retried a bounded number of times first.
    assert page.text == '<div id="main"></div>'
    assert len(client.calls) == backend.MAX_ATTEMPTS


def test_ddg_fetch_retries_past_challenge(monkeypatch):
    """A challenge page followed by a real one should return the real results."""
    backend = DdgHtmlBackend()
    backend.RETRY_DELAY = 0

    class _FlakyClient:
        def __init__(self):
            self.n = 0

        def post(self, url, headers=None, cookies=None, data=None):
            self.n += 1
            if self.n == 1:
                return _FakeResponse("<html>anomaly challenge</html>", 202)
            return _FakeResponse(DDG_SAMPLE, 200)

    client = _FlakyClient()
    page = backend.fetch(client, DDG_HTML_URL + "raspberry%20pi%205", headers={})
    assert client.n == 2
    assert page.status_code == 200
    assert page.text.count('class="ZINbbc"') == 2
