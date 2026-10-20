"""Regression tests for app.filter.extract_q.

extract_q gates extraction on the *href* but reads the *query string*; when a
link's query has a parameter (or fragment) whose value itself contains
``?q=``/``&q=`` there is no top-level ``q`` key and the old implementation
raised ``KeyError``, turning any such result link into a 500.
"""

import httpx
from bs4 import BeautifulSoup

from app import app
from app.filter import Filter, extract_q
from app.models.config import Config
from app.models.endpoint import Endpoint
from app.request import Request
from app.utils.session import generate_key
from test.conftest import demo_config


def test_extract_q_normal():
    assert extract_q('q=hello', 'http://x/?q=hello') == 'hello'


def test_extract_q_blank_value():
    assert extract_q('q=', 'http://x/?q=') == ''


def test_extract_q_absent_returns_empty():
    assert extract_q('a=1', 'http://x/?a=1') == ''


def test_extract_q_nested_q_in_param_value_does_not_raise():
    # No top-level q, but the href check passes because the value of 'url'
    # contains '?q='.
    q_str = 'url=https://other.com/?q=hi'
    href = 'https://example.com/?url=https://other.com/?q=hi'
    assert extract_q(q_str, href) == ''


def test_extract_q_nested_q_in_fragment_does_not_raise():
    assert extract_q('a=1', 'https://example.com/p?a=1#?q=') == ''


def _bad_link_html():
    return ('<html><body><div id="main"><div class="ZINbbc"><div>'
            '<a href="https://example.com/?url=https://other.com/?q=hi">'
            'A result link</a><span class="VwiC3b">snippet</span>'
            '</div></div></div></body></html>')


def test_filter_clean_handles_link_with_nested_q():
    with app.app_context():
        soup = Filter(
            user_key=generate_key(),
            config=Config(**demo_config),
        ).clean(BeautifulSoup(_bad_link_html(), 'html.parser'))
    assert soup is not None


def test_search_route_survives_link_with_nested_q(client, monkeypatch):
    def fake_send(self, base_url='', query='', attempt=0,
                  force_mobile=False, user_agent=''):
        req = httpx.Request('GET', 'https://www.google.com/search')
        return httpx.Response(200, request=req, text=_bad_link_html())

    monkeypatch.setattr(Request, 'send', fake_send)
    rv = client.get(f'/{Endpoint.search}?q=test')
    assert rv._status_code == 200
