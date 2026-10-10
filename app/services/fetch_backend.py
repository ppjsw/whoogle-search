"""Pluggable search fetch backends.

Whoogle historically hardcoded a single fetch path: a GET against
``https://www.google.com/search?gbv=1`` parsed as Google's own DOM. Google
stopped serving organic results to non-JavaScript clients (see the project
charter), so the fetch layer is now selectable via ``WHOOGLE_FETCH_BACKEND``.

A backend owns two things:

* **fetching** - which URL/endpoint and HTTP method to use, and
* **parsing/normalisation** - turning the engine's response into the
  Google-shaped DOM that Whoogle's :class:`app.filter.Filter` and the JSON
  route already consume (a ``div#main`` containing result ``div.ZINbbc``
  blocks with an ``h3`` title/link and a ``span.VwiC3b`` snippet).

Backends:

``google-html`` (default)
    The original path: Google's ``gbv=1`` HTML, passed through unchanged. It
    is dead upstream (Google returns a JS wall) but the switch must not alter
    its behaviour, so the raw response is returned verbatim.

``ddg-html``
    ``https://html.duckduckgo.com/html/``, no browser required. DuckDuckGo
    returns a JS "anomaly" challenge page to a plain GET from a server, so the
    search is issued as a POST (verified working from this host). The result
    blocks are then rewritten into the Google-shaped DOM described above.

``google-headless``
    Reserved for a browser-rendered Google backend (a later step); not
    implemented here. Requesting it falls back to ``google-html`` with a
    warning rather than failing.
"""

import html
import os
import time
import urllib.parse as urlparse

from bs4 import BeautifulSoup

DEFAULT_FETCH_BACKEND = "google-html"

GOOGLE_HTML_URL = "https://www.google.com/search?gbv=1&q="
GOOGLE_IMAGE_URL = "https://www.google.com/search?udm=2&q="
DDG_HTML_URL = "https://html.duckduckgo.com/html/"

# DuckDuckGo's endpoint challenges non-browser clients. A modern desktop Chrome
# UA with minimal headers is served results reliably (verified from this host);
# the Google-oriented UA pool and Sec-Fetch-* headers Whoogle builds for Google
# trigger the challenge instead, so the backend ignores the passed-in headers.
DDG_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)
DDG_HEADERS = {
    "User-Agent": DDG_USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Google classes Whoogle's Filter/JSON route key off:
#   div.ZINbbc          -> result container (GClasses.result_class_a)
#   span.VwiC3b         -> snippet text (JSON route)
RESULT_DIV_CLASS = "ZINbbc"
SNIPPET_CLASS = "VwiC3b"


class FetchPage:
    """A minimal response-like wrapper.

    ``Request.send`` callers only ever read ``.text`` (and, for images, the
    status), so backends that rewrite the response can hand one of these back
    instead of an ``httpx.Response``.
    """

    def __init__(self, text, status_code=200, url=""):
        self.text = text
        self.status_code = status_code
        self.url = url


class FetchBackend:
    """Base class for search fetch backends."""

    name = "base"
    search_url = ""
    image_search_url = ""

    def fetch(self, http_client, url, headers=None, cookies=None):
        """Perform the search request.

        Args:
            http_client: the shared :class:`app.services.http_client.HttpxClient`
            url: the fully-built request URL (``search_url + query``)
            headers: request headers
            cookies: request cookies

        Returns:
            An object exposing ``.text`` with Whoogle-ready HTML.
        """
        raise NotImplementedError

    def parse(self, raw_html):
        """Normalise a raw engine response into Whoogle's Google-shaped DOM.

        The default is a no-op: engines whose markup Whoogle already understands
        (Google) are passed through unchanged.
        """
        return raw_html


class GoogleHtmlBackend(FetchBackend):
    """The original Google ``gbv=1`` HTML path (default, unchanged)."""

    name = "google-html"
    search_url = GOOGLE_HTML_URL
    image_search_url = GOOGLE_IMAGE_URL

    def fetch(self, http_client, url, headers=None, cookies=None):
        return http_client.get(url, headers=headers, cookies=cookies)


def _resolve_target(href):
    """Resolve a DuckDuckGo result href to its real destination URL.

    DuckDuckGo frequently wraps links as
    ``//duckduckgo.com/l/?uddg=<percent-encoded-target>&rut=...``. The real
    target lives in the ``uddg`` query parameter and must be unwrapped before
    the link is usable. Direct links (which the POST endpoint also returns) are
    passed through.
    """
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse.urlparse(href)
    if "duckduckgo.com" in parsed.netloc and (
        parsed.path.startswith("/l/") or parsed.path == "/l"
    ):
        target = urlparse.parse_qs(parsed.query).get("uddg")
        if target:
            return target[0]
    return href


class DdgHtmlBackend(FetchBackend):
    """DuckDuckGo's server-rendered HTML endpoint, no browser required."""

    name = "ddg-html"
    search_url = DDG_HTML_URL
    image_search_url = DDG_HTML_URL
    # DuckDuckGo intermittently answers with a JS "anomaly" challenge page
    # (HTTP 202, no result blocks) rather than results, so a query is retried a
    # couple of times. This is a personal instance: a few extra requests per
    # query is fine.
    MAX_ATTEMPTS = 3
    RETRY_DELAY = 1.0

    def fetch(self, http_client, url, headers=None, cookies=None):
        # The raw query is everything between the base URL and the first
        # Whoogle-appended param ('&...'). gen_query percent-encodes the user
        # query, so any literal '&' inside it is already '%26'.
        raw_query = ""
        if url.startswith(self.search_url):
            raw_query = url[len(self.search_url):]
        raw_query = urlparse.unquote(raw_query.split("&", 1)[0])

        # A plain GET is challenged with a JS "anomaly" page (HTTP 202). The
        # search must be a POST to the same endpoint, with DuckDuckGo-friendly
        # headers (its own UA/Accept supersede whatever the caller passed).
        request_headers = dict(DDG_HEADERS)
        response = None
        normalized = '<div id="main"></div>'
        for attempt in range(self.MAX_ATTEMPTS):
            response = http_client.post(
                self.search_url,
                headers=request_headers,
                data={"q": raw_query},
            )
            normalized = self.parse(response.text)
            if f'class="{RESULT_DIV_CLASS}"' in normalized:
                break
            if attempt < self.MAX_ATTEMPTS - 1:
                time.sleep(self.RETRY_DELAY)

        return FetchPage(
            text=normalized,
            status_code=response.status_code if response is not None else 0,
            url=self.search_url,
        )

    def parse(self, raw_html):
        """Rewrite a DuckDuckGo result page into Whoogle's Google-shaped DOM."""
        if not raw_html:
            return '<div id="main"></div>'

        soup = BeautifulSoup(raw_html, "html.parser")
        parts = ['<div id="main">']

        for link in soup.find_all("a", class_="result__a"):
            href = _resolve_target(link.get("href", ""))
            if not href.startswith("http"):
                continue

            title = link.get_text(" ", strip=True)

            container = link.find_parent("div", class_="result__body")
            if container is None:
                container = link.find_parent("div", class_="web-result")

            snippet = ""
            if container is not None:
                snippet_el = container.find(class_="result__snippet")
                if snippet_el is not None:
                    snippet = snippet_el.get_text(" ", strip=True)

            parts.append(
                f'<div class="{RESULT_DIV_CLASS}">'
                f'<h3><a href="{html.escape(href, quote=True)}">'
                f"{html.escape(title)}</a></h3>"
                f'<span class="{SNIPPET_CLASS}">{html.escape(snippet)}</span>'
                "</div>"
            )

        parts.append("</div>")
        return "".join(parts)


_BACKENDS = {
    GoogleHtmlBackend.name: GoogleHtmlBackend,
    DdgHtmlBackend.name: DdgHtmlBackend,
}


def get_backend(name=None):
    """Return a fetch backend instance for ``name``.

    Reads ``WHOOGLE_FETCH_BACKEND`` when ``name`` is not given. Unknown or
    not-yet-implemented values (e.g. ``google-headless``) fall back to
    ``google-html`` with a warning, so a stale config can never break startup.
    """
    if name is None:
        name = os.getenv("WHOOGLE_FETCH_BACKEND", DEFAULT_FETCH_BACKEND)
    name = (name or DEFAULT_FETCH_BACKEND).strip().lower()

    backend_cls = _BACKENDS.get(name)
    if backend_cls is None:
        print(
            f"Warning: unknown WHOOGLE_FETCH_BACKEND={name!r}; "
            f"falling back to {DEFAULT_FETCH_BACKEND}."
        )
        backend_cls = GoogleHtmlBackend
    return backend_cls()
