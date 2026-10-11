import json

import httpx
import pytest

from app.models.endpoint import Endpoint
from app.utils import search as search_mod
from app.utils import json_api

RESULT_DIV = (
    '<div class="ZINbbc xpd">'
    '<a href="{href}"><h3>{title}</h3></a>'
    '<div class="VwiC3b">{snippet}</div>'
    '</div>'
)


def _stub_search(monkeypatch, html, upstream_status=200, upstream_raw=None):
    """Replace Search's query/response so no network access happens."""
    def fake_new_query(self):
        self.query = 'test'
        return self.query

    def fake_generate(self):
        self.upstream_status = upstream_status
        self.upstream_raw = upstream_raw
        return html

    monkeypatch.setattr(search_mod.Search, 'new_search_query', fake_new_query)
    monkeypatch.setattr(search_mod.Search, 'generate_response', fake_generate)


def test_captcha_json_block(client, monkeypatch):
    def fake_new_query(self):
        self.query = 'test'
        return self.query

    def fake_generate(self):
        # Inject a captcha marker into HTML so route returns 503 JSON
        return '<div>div class="g-recaptcha"</div>'

    monkeypatch.setattr(search_mod.Search, 'new_search_query', fake_new_query)
    monkeypatch.setattr(search_mod.Search, 'generate_response', fake_generate)

    rv = client.get(f'/{Endpoint.search}?q=test&format=json')
    assert rv._status_code == 503
    data = json.loads(rv.data)
    assert data['blocked'] is True
    assert 'error_message' in data
    # New: machine-readable error type and a uniform empty results array.
    assert data['error_type'] == 'blocked'
    assert data['results'] == []
    assert data['count'] == 0


def test_json_success_shape(client, monkeypatch):
    html = '<div id="main">' + RESULT_DIV.format(
        href="https://example.com/a", title="Alpha", snippet="First.") + \
        '</div>'
    _stub_search(monkeypatch, html)

    rv = client.get(f'/{Endpoint.search}?q=test&format=json')
    assert rv._status_code == 200
    data = json.loads(rv.data)
    assert data['count'] == 1
    result = data['results'][0]
    assert result['position'] == 1
    assert result['title'] == 'Alpha'
    assert result['href'] == 'https://example.com/a'
    assert result['snippet'] == 'First.'
    # Legacy fields preserved.
    assert 'text' in result
    assert 'content' in result


def test_json_limit_parameter(client, monkeypatch):
    html = '<div id="main">' + "".join(
        RESULT_DIV.format(href=f"https://example.com/{i}",
                          title=f"T{i}", snippet=f"S{i}")
        for i in range(4)) + '</div>'
    _stub_search(monkeypatch, html)

    rv = client.get(f'/{Endpoint.search}?q=test&format=json&limit=2')
    assert rv._status_code == 200
    data = json.loads(rv.data)
    assert data['count'] == 2
    assert len(data['results']) == 2


def test_json_limit_invalid_returns_400(client, monkeypatch):
    _stub_search(monkeypatch, '<div id="main"></div>')

    rv = client.get(f'/{Endpoint.search}?q=test&format=json&limit=abc')
    assert rv._status_code == 400
    data = json.loads(rv.data)
    assert data['error'] is True
    assert data['error_type'] == 'invalid_request'
    assert data['results'] == []


def test_json_noresults_is_not_blocked(client, monkeypatch):
    # A genuine empty result set: HTTP 200, count 0, and crucially *no*
    # error/blocked key.
    _stub_search(monkeypatch, '<div id="main"></div>', upstream_status=200)

    rv = client.get(f'/{Endpoint.search}?q=test&format=json')
    assert rv._status_code == 200
    data = json.loads(rv.data)
    assert data['count'] == 0
    assert data['results'] == []
    assert 'error' not in data
    assert 'blocked' not in data


def test_json_blocked_is_distinguishable(client, monkeypatch):
    # Same empty body, but the backend reported a challenge status (429).
    _stub_search(monkeypatch, '<div id="main"></div>', upstream_status=429)

    rv = client.get(f'/{Endpoint.search}?q=test&format=json')
    assert rv._status_code == 503
    data = json.loads(rv.data)
    assert data['blocked'] is True
    assert data['error_type'] == 'blocked'
    assert data['results'] == []


def test_json_raw_wall_marker_is_blocked(client, monkeypatch):
    # google-html's JS wall: the filtered soup is empty, but the *raw* upstream
    # body carries the enablejs marker (the Filter strips it). Must be reported
    # as blocked, never as a genuine empty result set.
    _stub_search(
        monkeypatch,
        '<div id="main"></div>',
        upstream_status=200,
        upstream_raw='<meta content="0;url=/httpservice/retry/enablejs" '
                     'http-equiv="refresh">')

    rv = client.get(f'/{Endpoint.search}?q=test&format=json')
    assert rv._status_code == 503
    data = json.loads(rv.data)
    assert data['blocked'] is True
    assert data['error_type'] == 'blocked'
    assert data['results'] == []


def test_json_upstream_unavailable(client, monkeypatch):
    _stub_search(monkeypatch, '<div id="main"></div>', upstream_status=503)

    rv = client.get(f'/{Endpoint.search}?q=test&format=json')
    assert rv._status_code == 502
    data = json.loads(rv.data)
    assert data['error_type'] == 'upstream_error'
    assert data['results'] == []


def test_json_timeout_shape(client, monkeypatch):
    def fake_new_query(self):
        self.query = 'test'
        return self.query

    def fake_generate(self):
        raise httpx.TimeoutException('boom')

    monkeypatch.setattr(search_mod.Search, 'new_search_query', fake_new_query)
    monkeypatch.setattr(search_mod.Search, 'generate_response', fake_generate)

    rv = client.get(f'/{Endpoint.search}?q=test&format=json')
    assert rv._status_code == 504
    data = json.loads(rv.data)
    assert data['error'] is True
    assert data['error_type'] == 'timeout'
    assert data['results'] == []


def test_json_resolves_google_redirect_token(client, monkeypatch):
    href = ("https://www.google.com/url?q=https%3A%2F%2Freal.example%2Fp"
            "&sa=U")
    html = '<div id="main">' + RESULT_DIV.format(
        href=href, title="Real", snippet="S") + '</div>'
    _stub_search(monkeypatch, html)

    rv = client.get(f'/{Endpoint.search}?q=test&format=json')
    data = json.loads(rv.data)
    assert data['results'][0]['href'] == 'https://real.example/p'
    assert not json_api.is_redirect_token(data['results'][0]['href'])
