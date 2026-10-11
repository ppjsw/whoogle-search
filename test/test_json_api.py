"""Unit tests for the agent-facing JSON contract (app/utils/json_api.py)."""

import json

from bs4 import BeautifulSoup

from app.utils import json_api


def _soup(html):
    return BeautifulSoup(html, "html.parser")


RESULT_DIV = (
    '<div class="ZINbbc xpd">'
    '<a href="{href}"><h3>{title}</h3></a>'
    '<div class="VwiC3b">{snippet}</div>'
    '</div>'
)


# -- schema ------------------------------------------------------------------

def test_result_fields_and_positions():
    html = (
        '<div id="main">'
        + RESULT_DIV.format(
            href="https://example.com/a", title="Alpha", snippet="First.")
        + RESULT_DIV.format(
            href="https://example.org/b", title="Beta", snippet="Second.")
        + '</div>'
    )
    payload = json_api.success_payload("q", "", html)
    assert payload["query"] == "q"
    assert payload["search_type"] == ""
    assert payload["count"] == 2
    assert [r["position"] for r in payload["results"]] == [1, 2]
    first = payload["results"][0]
    for key in ("position", "title", "href", "snippet", "content", "text"):
        assert key in first
    assert first["title"] == "Alpha"
    assert first["href"] == "https://example.com/a"
    assert first["snippet"] == "First."
    # snippet/content/text: snippet is canonical, content an identical alias.
    assert first["content"] == first["snippet"]
    assert isinstance(first["text"], str)


def test_legacy_fields_are_preserved():
    # Consumers depend on title/href (and historically text/content) existing.
    html = ('<div id="main">'
            + RESULT_DIV.format(
                href="https://example.com/a", title="A", snippet="S")
            + '</div>')
    payload = json_api.success_payload("q", "", html)
    result = payload["results"][0]
    assert set(["title", "href", "text", "content"]).issubset(result.keys())


# -- limit -------------------------------------------------------------------

def test_limit_truncates_results():
    html = '<div id="main">' + "".join(
        RESULT_DIV.format(href=f"https://example.com/{i}",
                          title=f"T{i}", snippet=f"S{i}")
        for i in range(5)) + '</div>'
    assert len(json_api.success_payload("q", "", html)["results"]) == 5
    assert len(
        json_api.success_payload("q", "", html, limit=2)["results"]) == 2


def test_limit_above_available_does_not_error():
    html = ('<div id="main">'
            + RESULT_DIV.format(
                href="https://example.com/a", title="A", snippet="S")
            + '</div>')
    payload = json_api.success_payload("q", "", html, limit=50)
    assert payload["count"] == 1


def test_parse_limit():
    assert json_api.parse_limit(None) == (None, None)
    assert json_api.parse_limit("") == (None, None)
    assert json_api.parse_limit("3") == (3, None)
    limit, err = json_api.parse_limit("0")
    assert limit is None and err
    limit, err = json_api.parse_limit("-1")
    assert limit is None and err
    limit, err = json_api.parse_limit("abc")
    assert limit is None and err


# -- no-redirect-token invariant ---------------------------------------------

def test_google_url_token_is_resolved_not_emitted():
    href = ("https://www.google.com/url?q=https%3A%2F%2Freal.example%2Fp"
            "&sa=U&ved=2ahUKE")
    html = '<div id="main">' + RESULT_DIV.format(
        href=href, title="Real", snippet="S") + '</div>'
    payload = json_api.success_payload("q", "", html)
    hrefs = [r["href"] for r in payload["results"]]
    assert hrefs == ["https://real.example/p"]
    assert not any(json_api.is_redirect_token(h) for h in hrefs)


def test_goto_token_is_unresolvable_and_dropped():
    # /goto?url=<opaque token> cannot be unwrapped without a network hop, so
    # the result must be dropped rather than emitted as a token href.
    href = "https://www.google.com/goto?url=AbCdefOpaqueToken"
    html = '<div id="main">' + RESULT_DIV.format(
        href=href, title="Dropped", snippet="S") + '</div>'
    payload = json_api.success_payload("q", "", html)
    assert payload["count"] == 0


def test_duckduckgo_uddg_token_is_resolved():
    href = ("//duckduckgo.com/l/?uddg="
            "https%3A%2F%2Fddg-target.example%2Fx&rut=zzz")
    html = '<div id="main">' + RESULT_DIV.format(
        href=href, title="DDG", snippet="S") + '</div>'
    payload = json_api.success_payload("q", "", html)
    assert payload["results"][0]["href"] == "https://ddg-target.example/x"


def test_is_redirect_token():
    assert json_api.is_redirect_token(
        "https://www.google.com/url?q=https://x.example")
    assert json_api.is_redirect_token("https://www.google.com/goto?url=tok")
    assert json_api.is_redirect_token("//duckduckgo.com/l/?uddg=https%3A%2F%2Fx")
    assert not json_api.is_redirect_token("https://example.com/page?q=1")
    assert not json_api.is_redirect_token("")


def test_resolve_redirect_token():
    assert json_api.resolve_redirect_token(
        "https://www.google.com/url?q=https%3A%2F%2Fx.example%2Fa&sa=U"
    ) == "https://x.example/a"
    assert json_api.resolve_redirect_token(
        "//duckduckgo.com/l/?uddg=https%3A%2F%2Fy.example"
    ) == "https://y.example"
    # Opaque token: not resolvable offline.
    assert json_api.resolve_redirect_token(
        "https://www.google.com/goto?url=tok") == ""


# -- fallback path -----------------------------------------------------------

def test_fallback_plain_links():
    # No recognised result container -> link-text fallback must still work.
    html = ('<div id="main">'
            '<a href="https://example.com/page">Example Page</a>'
            '<a href="/relative">Relative</a>'
            '<a href="https://example.org/other">Other</a>'
            '</div>')
    payload = json_api.success_payload("q", "", html)
    hrefs = [r["href"] for r in payload["results"]]
    assert hrefs == ["https://example.com/page", "https://example.org/other"]
    assert not any(h.endswith("/relative") for h in hrefs)
    assert payload["results"][0]["title"] == "Example Page"
    assert payload["results"][0]["content"] == ""


# -- blocked vs no-results ---------------------------------------------------

def test_classify_empty_genuine_no_results():
    assert json_api.classify_empty(200, '<div id="main"></div>') is None


def test_classify_empty_blocked_by_status():
    assert json_api.classify_empty(
        429, '<div id="main"></div>') == json_api.ERROR_BLOCKED
    assert json_api.classify_empty(
        202, '<div id="main"></div>') == json_api.ERROR_BLOCKED


def test_classify_empty_upstream():
    assert json_api.classify_empty(
        503, '<div id="main"></div>') == json_api.ERROR_UPSTREAM
    assert json_api.classify_empty(
        0, '<div id="main"></div>') == json_api.ERROR_UPSTREAM


def test_classify_empty_wall_marker():
    assert json_api.classify_empty(
        200, '<meta http-equiv=refresh content="0;url=/httpservice/retry/'
             'enablejs">') == json_api.ERROR_BLOCKED


def test_classify_ignores_wall_marker_when_results_present():
    # A normal SERP's bootstrap JS mentions /sorry/; detection must not fire
    # when the page actually yielded result blocks.
    html = ('<div class="ZINbbc"><a href="https://x.example"><h3>X</h3></a>'
            '</div><script>indexOf("/sorry/index")</script>')
    assert json_api.classify_empty(200, html) is None


# -- error payload -----------------------------------------------------------

def test_error_payload_shape():
    payload = json_api.error_payload("q", json_api.ERROR_BLOCKED, "blocked!",
                                     blocked=True)
    assert payload["error"] is True
    assert payload["error_type"] == "blocked"
    assert payload["error_message"] == "blocked!"
    assert payload["blocked"] is True
    assert payload["results"] == []
    assert payload["count"] == 0
    # Must be JSON-serialisable.
    json.dumps(payload)


def test_error_payload_without_blocked_flag():
    payload = json_api.error_payload("q", json_api.ERROR_TIMEOUT, "timeout")
    assert "blocked" not in payload
    assert payload["results"] == []
