"""The agent-facing JSON contract for Whoogle search results.

This module is the single source of truth for the ``format=json`` /
``Accept: application/json`` response produced by the ``/search`` route. It is
deliberately free of Flask/network dependencies so the contract can be unit
tested in isolation and reused by consumers (e.g. the planned MCP server).

Contract summary (see ``docs/JSON-API.md`` for the full document):

* Request: ``GET /search?q=<query>&format=json`` or
  ``Accept: application/json``. An optional ``limit=<int>`` caps the number of
  returned results.
* Success (HTTP 200): ``{query, search_type, count, results[]}`` where each
  result is ``{position, title, href, snippet, content, text}``.
  - ``href`` is always a *fully resolved absolute real URL* (never a Google
    ``/url?q=`` or ``/goto?url=`` redirect token, never a DuckDuckGo
    ``/l/?uddg=`` wrapper).
  - ``snippet`` is the canonical agent-facing snippet; ``content`` is a legacy
    alias carrying the identical value, and ``text`` is the legacy
    title+snippet blob. All three are kept for backwards compatibility.
* No results (HTTP 200): the success shape with ``count == 0`` and
  ``results == []`` - and **no** ``error``/``blocked`` key.
* Errors carry a machine-readable ``error_type`` and always an empty
  ``results`` array so callers can read ``.results`` uniformly:
  - ``blocked`` (HTTP 503): an anti-bot / CAPTCHA challenge page.
  - ``timeout`` (HTTP 504): the upstream engine timed out.
  - ``upstream_error`` (HTTP 502): the upstream engine was unreachable or
    failed.
  - ``invalid_request`` (HTTP 400): a bad parameter (e.g. non-integer limit).
  - ``internal_error`` (HTTP 500): an unexpected server error.
"""

import re
import urllib.parse as urlparse

# Bump when the response shape changes in a backwards-incompatible way.
CONTRACT_VERSION = 1

# Honest upper bound for a single results page: Google now serves only 8-9
# natural results per SERP (the rest of the page is AI-overview / people-also-
# ask widgets), so a `limit` above this cannot be satisfied by one page. A
# `limit` can only *reduce* the result count below what the engine returned.
OBSERVED_MAX_RESULTS_PER_PAGE = 9

# -- error types ------------------------------------------------------------

ERROR_BLOCKED = "blocked"
ERROR_TIMEOUT = "timeout"
ERROR_UPSTREAM = "upstream_error"
ERROR_INVALID = "invalid_request"
ERROR_INTERNAL = "internal_error"
ERROR_CSE = "cse_error"
ERROR_CSE_QUOTA = "cse_quota"

# Upstream HTTP statuses that mean "the engine blocked us" (anti-bot page),
# versus "the engine is unavailable". Verified live on this host:
#   google-headless challenge -> FetchPage(status_code=429)
#   google-headless unavailable -> FetchPage(status_code=503)
#   ddg-html challenge -> HTTP 202 with no result blocks
BLOCKED_UPSTREAM_STATUSES = (202, 429)
UNAVAILABLE_UPSTREAM_STATUSES = (0, 503)

# Markers that identify a wall page when the response carries no result blocks.
# 'retry/enablejs' is Google's gbv=1 "enable JavaScript" interstitial; the
# others are the classic challenge forms. A *normal* SERP's own bootstrap JS
# also contains the string '/sorry/', so detection must always be gated on the
# page yielding zero result blocks (see :func:`classify_empty`).
WALL_MARKERS = (
    "retry/enablejs",
    'id="captcha-form"',
    "g-recaptcha",
)

# -- link resolution / anti-token invariant ---------------------------------

_GOOGLE_REDIRECT_RE = re.compile(
    r"^https?://(www\.)?google\.[^/]+/(url|goto)\b", re.IGNORECASE)
_DDG_REDIRECT_RE = re.compile(
    r"^https?://(www\.)?duckduckgo\.com/l/?\?", re.IGNORECASE)


def _absolutize(href):
    """Return an absolute form of ``href`` (scheme-relative -> https)."""
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    return href


def is_redirect_token(href):
    """True if ``href`` is an engine redirect wrapper, not a real target URL.

    Whoogle's contract forbids these in ``results[].href``: an agent cannot use
    an opaque ``/goto?url=<token>`` (the token is not even a URL), and a
    ``/url?q=`` wrapper leaks the engine as an intermediary.
    """
    href = _absolutize(href)
    if not href:
        return False
    return bool(_GOOGLE_REDIRECT_RE.match(href)
                or _DDG_REDIRECT_RE.match(href))


def resolve_redirect_token(href):
    """Best-effort extraction of the real target from a redirect wrapper.

    Returns the absolute target URL, or ``""`` when it cannot be resolved
    without a network hop (Google's opaque ``/goto?url=<token>`` form).
    """
    href = _absolutize(href)
    if not href:
        return ""
    if "/url?q=" in href:
        target = href.split("/url?q=", 1)[1].split("&", 1)[0]
        return urlparse.unquote(target)
    if _DDG_REDIRECT_RE.match(href):
        target = urlparse.parse_qs(urlparse.urlparse(href).query).get("uddg")
        if target:
            return target[0]
    return ""


def _safe_href(href):
    """Return a usable absolute href, enforcing the no-redirect-token rule.

    Returns ``""`` (drop the result) if the href is relative, non-http, or a
    redirect token that cannot be resolved to a real URL.
    """
    href = _absolutize(href)
    if not href.startswith("http://") and not href.startswith("https://"):
        return ""
    if is_redirect_token(href):
        resolved = _absolutize(resolve_redirect_token(href))
        if (not resolved.startswith("http")
                or is_redirect_token(resolved)):
            print("Warning: dropping result with unresolvable redirect "
                  "token: %s" % href[:120])
            return ""
        return resolved
    return href


# -- text helpers -----------------------------------------------------------

def clean_text_spacing(text):
    """Normalise whitespace and fix spacing artefacts from HTML extraction."""
    if not text:
        return text

    text = re.sub(r"\s+", " ", text)
    # Fix domain names: "weather .com" -> "weather.com"
    text = re.sub(r"\s+\.([a-zA-Z]{2,})\b", r".\1", text)
    # Fix www/http/https patterns
    text = re.sub(r"\b(www|http|https)\s+\.", r"\1.", text)
    # Fix spaces before common punctuation
    text = re.sub(r"\s+([,;:])", r"\1", text)
    return text.strip()


def _pick_title(div, link):
    h3_tag = div.find("h3")
    if h3_tag:
        return clean_text_spacing(h3_tag.get_text(separator=" ", strip=True))
    title_span = div.find("span", class_="CVA68e")
    if title_span:
        return clean_text_spacing(
            title_span.get_text(separator=" ", strip=True))
    if link:
        return clean_text_spacing(link.get_text(separator=" ", strip=True))
    return ""


_SNIPPET_SELECTORS = (
    {"class_": "VwiC3b"},   # standard snippet
    {"class_": "FrIlee"},   # alternative snippet class
    {"class_": "s"},        # another snippet class
    {"class_": "st"},       # legacy snippet class
)


def _pick_snippet(div):
    for selector in _SNIPPET_SELECTORS:
        snippet_elem = div.find("span", selector) or div.find("div", selector)
        if not snippet_elem:
            continue
        content = clean_text_spacing(
            snippet_elem.get_text(separator=" ", strip=True))
        # Reject a bare URL breadcrumb masquerading as a snippet.
        if content and not content.startswith("www.") and "›" not in content:
            return content
    return ""


def _make_result(position, href, title, content, text):
    return {
        "position": position,
        "title": title,
        "href": href,
        "snippet": content,   # canonical agent-facing field
        "content": content,   # legacy alias (identical value)
        "text": text,         # legacy: full container text
    }


def extract_results(soup, limit=None):
    """Extract the documented result list from a filtered result soup.

    ``soup`` is the same post-:class:`~app.filter.Filter` BeautifulSoup the
    HTML view is rendered from, so the JSON and HTML views can never diverge
    in which results they see.

    Every emitted ``href`` passes :func:`_safe_href`, which guarantees the
    no-redirect-token invariant.
    """
    results = []
    seen = set()

    result_divs = soup.find_all("div", class_=["ZINbbc", "ezO2md"])
    if result_divs:
        for div in result_divs:
            link = None
            for a in div.find_all("a", href=True):
                if (a["href"].startswith("http")
                        or a["href"].startswith("//")):
                    link = a
                    break
            if not link:
                continue

            href = _safe_href(link["href"])
            if not href or href in seen:
                continue

            text = clean_text_spacing(
                div.get_text(separator=" ", strip=True))
            if not text:
                continue

            title = _pick_title(div, link)
            content = _pick_snippet(div)
            if not content and title:
                if text.startswith(title):
                    content = text[len(title):].strip()
                else:
                    content = text
            elif not content:
                content = text

            seen.add(href)
            results.append(_make_result(
                len(results) + 1, href, title, content, text))
    else:
        # Fallback: engines/layouts that yield no recognised result container.
        for a in soup.find_all("a", href=True):
            href = _safe_href(a["href"])
            if not href or href in seen:
                continue
            text = clean_text_spacing(
                a.get_text(separator=" ", strip=True))
            if not text:
                continue
            seen.add(href)
            results.append(_make_result(
                len(results) + 1, href, text, "", text))

    if limit is not None:
        results = results[:limit]
    return results


# -- payload builders -------------------------------------------------------

def success_payload(query, search_type, html, limit=None):
    """Build the documented success payload from filtered result HTML."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(str(html), "html.parser")
    results = extract_results(soup, limit=limit)
    return {
        "query": query,
        "search_type": search_type or "",
        "count": len(results),
        "results": results,
    }


def error_payload(query, error_type, message, blocked=False):
    """Build the documented error payload.

    Always includes an empty ``results`` array and ``count`` of 0 so a caller
    can read ``.results`` without branching on the presence of the key. The
    legacy ``error``/``error_message``/``blocked`` keys are preserved.
    """
    payload = {
        "query": query,
        "error": True,
        "error_type": error_type,
        "error_message": message,
        "count": 0,
        "results": [],
    }
    if blocked:
        payload["blocked"] = True
    return payload


def classify_empty(upstream_status, html):
    """Classify a zero-result response: ``blocked``, ``upstream`` or ``None``.

    ``None`` means a genuine, empty result set (HTTP 200, no error key). This
    is the crux of the contract: a challenge page must never look like
    "no results". Detection requires zero result blocks, because a normal
    SERP's own JavaScript also references the challenge wording.
    """
    if "class=\"ZINbbc\"" in str(html):
        return None
    if upstream_status in BLOCKED_UPSTREAM_STATUSES:
        return ERROR_BLOCKED
    if upstream_status in UNAVAILABLE_UPSTREAM_STATUSES:
        return ERROR_UPSTREAM
    if any(marker in str(html) for marker in WALL_MARKERS):
        return ERROR_BLOCKED
    return None


def parse_limit(raw):
    """Validate a ``limit`` request value.

    Returns ``(limit, None)`` on success, or ``(None, message)`` on failure.
    An absent/empty value yields ``(None, None)`` (no cap).
    """
    if raw is None or str(raw).strip() == "":
        return None, None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None, "'limit' must be a positive integer"
    if value <= 0:
        return None, "'limit' must be a positive integer"
    return value, None


def wants_json(request):
    """True when the caller asked for the JSON representation."""
    return (
        request.args.get("format") == "json"
        or "application/json" in request.headers.get("Accept", "")
        or "application/*+json" in request.headers.get("Accept", "")
    )
