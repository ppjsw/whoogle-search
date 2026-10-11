#!/usr/bin/env python3
"""Whoogle Search MCP server (stdio transport).

Wraps a running Whoogle instance's JSON search endpoint
(``GET /search?q=<query>&format=json``) as MCP tools, so an AI agent can
search with a tool call instead of hand-rolling HTTP.

Design notes
------------
* **Transport: MCP stdio** - newline-delimited JSON-RPC 2.0 on stdin/stdout.
  Stdio is the right transport for a local, single-user tool: no listening
  port, no auth surface, no network exposure. The agent host spawns this
  process as a child and talks to it over pipes. (SSE/HTTP transports were
  rejected because they would require binding a socket, which this tool does
  not do.)
* **Dependencies: none beyond the Python 3.8+ standard library.** Only
  ``json``, ``sys``, ``os``, ``urllib``, ``socket``, ``ipaddress`` and
  ``html.parser`` are imported. This keeps the wrapper immune to MCP-SDK
  version churn and runnable anywhere a ``python3`` exists. The optional
  conformance test (``test/``) uses the official ``mcp`` Python SDK as a
  *client*; the server itself never imports it.
* This process **binds nothing**; it only makes *outbound* HTTP requests to
  the configured Whoogle instance (and, for ``fetch_page``, to the target
  URL subject to the safety rules below).

Contract tolerance
------------------
The stable core this server relies on is small: a JSON body with a list of
results where each entry carries a title, a resolved real target URL, and a
snippet. The current Whoogle backend emits ``href`` / ``text`` / ``title`` /
``content`` for those; the agent-facing contract hardening in issue #6 is
scheduled to standardise on ``url`` / ``snippet``. This server therefore
reads **both** spellings and never requires a field beyond title/url/snippet.
See README.md ("Contract tolerance") for the exact mapping.

Configuration (environment variables)
-------------------------------------
  WHOOGLE_URL                  Base URL of the Whoogle instance
                               (default: http://127.0.0.1:5000)
  WHOOGLE_TIMEOUT              Search request timeout, seconds (default: 30)
  WHOOGLE_MCP_MAX_RESULTS      Hard cap on results per search (default: 20)
  WHOOGLE_MCP_FETCH_MAX_BYTES  Max bytes read by fetch_page (default: 524288)
  WHOOGLE_MCP_FETCH_TIMEOUT    fetch_page timeout, seconds (default: 10)
  WHOOGLE_MCP_FETCH_MAX_CHARS  Max chars of extracted text (default: 4000)
"""

import ipaddress
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

SERVER_NAME = "whoogle-search-mcp"
SERVER_VERSION = "0.1.0"

# Protocol revisions this server can speak. We echo the client's requested
# revision when we support it, otherwise we answer with our preferred one.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = "2024-11-05"

# ---------------------------------------------------------------------------
# Errors surfaced to the agent as MCP tool errors (isError=true).
# ---------------------------------------------------------------------------


class ToolError(Exception):
    """A failure the agent should see as a structured tool error."""


# ---------------------------------------------------------------------------
# Backend client
# ---------------------------------------------------------------------------


def _base_url() -> str:
    return os.environ.get("WHOOGLE_URL", "http://127.0.0.1:5000").rstrip("/")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _normalise_result(item):
    """Map one backend result entry to {title, url, snippet}.

    Tolerates both the current field names (``href``/``content``) and the
    issue-#6 names (``url``/``snippet``). Returns None for entries without a
    usable URL.
    """
    if not isinstance(item, dict):
        return None
    url = item.get("url") or item.get("href") or item.get("link") or ""
    title = item.get("title") or item.get("text") or ""
    snippet = item.get("snippet")
    if snippet is None:
        snippet = item.get("content")
    if snippet is None:
        snippet = item.get("description") or ""
    if not isinstance(url, str):
        url = ""
    url = url.strip()
    if not url:
        return None
    return {
        "title": str(title or "").strip(),
        "url": url,
        "snippet": str(snippet or "").strip(),
    }


def _search_http(query: str):
    """Call the Whoogle JSON endpoint and return the parsed body (dict)."""
    base = _base_url()
    params = urllib.parse.urlencode({"q": query, "format": "json"})
    url = f"{base}/search?{params}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": f"{SERVER_NAME}/{SERVER_VERSION}",
        },
        method="GET",
    )
    timeout = _env_float("WHOOGLE_TIMEOUT", 30.0)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.getcode()
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
        raise ToolError(
            f"Whoogle backend unreachable at {base} "
            f"(is the instance running and is WHOOGLE_URL correct?): {exc}"
        )

    text = raw.decode("utf-8", errors="replace")
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        raise ToolError(
            "Whoogle backend did not return JSON. "
            f"HTTP {status}. First bytes: {text[:200]!r}"
        )

    if status != 200:
        # The backend's documented error shape (issue #6) carries a human
        # message; surface whatever it gave us.
        msg = ""
        if isinstance(body, dict):
            msg = body.get("error_message") or body.get("error") or ""
        raise ToolError(
            f"Whoogle backend returned HTTP {status}"
            + (f": {msg}" if msg else f": {text[:200]!r}")
        )

    if not isinstance(body, dict) or "results" not in body:
        keys = list(body.keys()) if isinstance(body, dict) else type(body).__name__
        raise ToolError(
            "Whoogle JSON response did not match the expected shape "
            f"(no top-level 'results' list). Top-level keys: {keys}"
        )
    return body


def run_search(query: str, limit=None):
    """Execute a search and return a normalised result dict."""
    if not isinstance(query, str) or not query.strip():
        raise ToolError("'query' must be a non-empty string")

    max_results = _env_int("WHOOGLE_MCP_MAX_RESULTS", 20)
    if limit is None:
        want = min(10, max_results)
    else:
        try:
            want = int(limit)
        except (TypeError, ValueError):
            raise ToolError("'limit' must be an integer")
        if want < 1:
            raise ToolError("'limit' must be >= 1")
        want = min(want, max_results)

    body = _search_http(query)

    results = []
    seen = set()
    for item in body.get("results") or []:
        norm = _normalise_result(item)
        if norm is None or norm["url"] in seen:
            continue
        seen.add(norm["url"])
        results.append(norm)

    # The backend cannot bound results yet (issue #6 adds a `limit` param);
    # limit is therefore applied here, client-side.
    truncated = results[:want]
    return {
        "query": body.get("query", query),
        "count": len(truncated),
        "total_available": len(results),
        "results": truncated,
    }


# ---------------------------------------------------------------------------
# fetch_page: bounded, SSRF-guarded text fetch. Deliberately NOT an open proxy.
# ---------------------------------------------------------------------------

MAX_REDIRECTS = 3
ALLOWED_SCHEMES = ("http", "https")
ALLOWED_CONTENT_TYPES = (
    "text/html",
    "application/xhtml",
    "text/plain",
    "application/json",
    "text/",
)


def _host_is_blocked(host: str) -> bool:
    """True if host resolves to a loopback/private/link-local/reserved IP."""
    if not host:
        return True
    h = host.strip("[]").lower()
    if h in ("localhost", "localhost.localdomain", "ip6-localhost"):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # Unresolvable -> we cannot prove it is public; refuse (fail closed).
        return True
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return True
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return True
    return False


def _check_url(url: str):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise ToolError(f"only http/https URLs are allowed, got scheme {parsed.scheme!r}")
    if _host_is_blocked(parsed.hostname or ""):
        raise ToolError(
            "refusing to fetch a localhost/private/link-local target "
            "(SSRF guard)"
        )


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect hop against the SSRF guard.

    urllib's built-in handler already caps redirect count; we only add the
    SSRF re-check on each hop's target.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _TextExtractor(HTMLParser):
    """Collect visible text, skipping script/style/head noise."""

    _SKIP = {"script", "style", "noscript", "template", "svg", "head"}
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chunks = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK:
            self._chunks.append("\n")

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self._chunks.append(data)

    def text(self) -> str:
        joined = "".join(self._chunks)
        lines = [" ".join(line.split()) for line in joined.splitlines()]
        return "\n".join(ln for ln in lines if ln)


def run_fetch_page(url: str, max_chars=None):
    """Fetch one URL through a bounded, SSRF-guarded reader and return text."""
    if not isinstance(url, str) or not url.strip():
        raise ToolError("'url' must be a non-empty string")
    url = url.strip()
    _check_url(url)

    timeout = _env_float("WHOOGLE_MCP_FETCH_TIMEOUT", 10.0)
    max_bytes = _env_int("WHOOGLE_MCP_FETCH_MAX_BYTES", 524288)
    out_chars = _env_int("WHOOGLE_MCP_FETCH_MAX_CHARS", 4000)
    if max_chars is not None:
        try:
            out_chars = min(int(max_chars), 20000)
        except (TypeError, ValueError):
            raise ToolError("'max_chars' must be an integer")

    opener = urllib.request.build_opener(_SafeRedirectHandler())
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"{SERVER_NAME}/{SERVER_VERSION}",
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
        },
        method="GET",
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype and not any(ctype.startswith(p) for p in ALLOWED_CONTENT_TYPES):
                raise ToolError(f"refusing non-text content type: {ctype!r}")
            raw = resp.read(max_bytes + 1)
            final_url = resp.geturl()
    except ToolError:
        raise
    except urllib.error.HTTPError as exc:
        raise ToolError(f"target returned HTTP {exc.code} for {url}")
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
        raise ToolError(f"could not fetch {url}: {exc}")

    truncated_bytes = len(raw) > max_bytes
    raw = raw[:max_bytes]
    charset = "utf-8"
    decoded = raw.decode(charset, errors="replace")

    if "html" in ctype or "<html" in decoded[:2000].lower():
        parser = _TextExtractor()
        try:
            parser.feed(decoded)
        except Exception:
            pass
        text = parser.text()
    else:
        text = decoded

    text_truncated = len(text) > out_chars
    text = text[:out_chars]
    return {
        "url": url,
        "final_url": final_url,
        "content_type": ctype or "unknown",
        "truncated_bytes": truncated_bytes,
        "truncated_text": text_truncated,
        "text": text,
    }


# ---------------------------------------------------------------------------
# MCP tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "search",
        "description": (
            "Search the web via the self-hosted Whoogle instance and return "
            "structured results (title, url, snippet). Use this instead of "
            "hand-rolling HTTP against the instance."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default 10).",
                    "minimum": 1,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fetch_page",
        "description": (
            "Fetch a URL returned by 'search' and return its visible text, so "
            "an agent can read a result without leaving the tool loop. "
            "Bounded and SSRF-guarded: http/https only, public hosts only, "
            "redirects re-checked, byte- and time-limited."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Absolute http/https URL to fetch.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters of extracted text (default 4000).",
                    "minimum": 1,
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
]

TOOL_HANDLERS = {
    "search": lambda args: run_search(args.get("query", ""), args.get("limit")),
    "fetch_page": lambda args: run_fetch_page(args.get("url", ""), args.get("max_chars")),
}


# ---------------------------------------------------------------------------
# JSON-RPC / MCP plumbing
# ---------------------------------------------------------------------------


def _render_search(result) -> str:
    lines = [
        f"{result['count']} result(s) for {result['query']!r} "
        f"(showing {result['count']} of {result['total_available']}):",
        "",
    ]
    for i, r in enumerate(result["results"], 1):
        lines.append(f"{i}. {r['title'] or '(untitled)'}")
        lines.append(f"   {r['url']}")
        if r["snippet"]:
            lines.append(f"   {r['snippet']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _render_fetch(result) -> str:
    head = f"# {result['url']}"
    if result["final_url"] != result["url"]:
        head += f" (final: {result['final_url']})"
    return f"{head}\n\n{result['text']}"


def _tool_result(text: str, structured=None, is_error: bool = False):
    payload = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if structured is not None and not is_error:
        payload["structuredContent"] = structured
    return payload


def _call_tool(name: str, arguments):
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return _tool_result(f"Unknown tool: {name}", is_error=True)
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _tool_result("tool arguments must be an object", is_error=True)
    try:
        result = handler(arguments)
    except ToolError as exc:
        return _tool_result(str(exc), is_error=True)
    except Exception as exc:  # defensive: never crash the loop on one bad call
        return _tool_result(f"internal error: {exc!r}", is_error=True)

    if name == "search":
        return _tool_result(_render_search(result), structured=result)
    return _tool_result(_render_fetch(result))


def _handle(msg):
    """Return a JSON-RPC response dict, or None for notifications."""
    if not isinstance(msg, dict):
        return _error(None, -32600, "invalid request")
    method = msg.get("method")
    msg_id = msg.get("id")
    is_notification = "id" not in msg

    if method == "initialize":
        params = msg.get("params") or {}
        requested = params.get("protocolVersion")
        version = (
            requested if requested in SUPPORTED_PROTOCOL_VERSIONS
            else DEFAULT_PROTOCOL_VERSION
        )
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
        return None if is_notification else _ok(msg_id, result)

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "ping":
        return None if is_notification else _ok(msg_id, {})

    if method == "tools/list":
        return None if is_notification else _ok(msg_id, {"tools": TOOLS})

    if method == "tools/call":
        params = msg.get("params") or {}
        result = _call_tool(params.get("name", ""), params.get("arguments", {}))
        return None if is_notification else _ok(msg_id, result)

    if is_notification:
        return None
    return _error(msg_id, -32601, f"method not found: {method}")


def _ok(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def main():
    """Read newline-delimited JSON-RPC from stdin, write responses to stdout."""
    stdin = sys.stdin
    stdout = sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            resp = _error(None, -32700, "parse error")
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()
            continue
        resp = _handle(msg)
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()


if __name__ == "__main__":
    main()
