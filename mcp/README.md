# Whoogle Search MCP server

An [MCP](https://modelcontextprotocol.io) server that exposes a running
**Whoogle Search** instance as tools an AI agent can call — so the agent
searches with a tool call instead of hand-rolling HTTP against the instance.

This directory is **self-contained**: it does not modify the Whoogle app, its
routes, its response shape, or its `requirements.txt`. It only *consumes* the
existing JSON endpoint (`GET /search?q=<query>&format=json`).

---

## Quick start

1. Start a Whoogle instance on localhost (it listens on `127.0.0.1:5000` by
   default; `app/routes.py` `run_app()` defaults `--host 127.0.0.1
   --port 5000`):

   ```sh
   # from the repo root, with the app's own venv/deps installed
   APP_ROOT=$PWD/app STATIC_FOLDER=$PWD/app/static \
   WHOOGLE_FETCH_BACKEND=google-headless \
   WHOOGLE_HEADLESS_CHROMIUM=/usr/bin/chromium \
   WHOOGLE_HEADLESS_PROFILE_DIR=/tmp/whoogle-profile \
   python3 -m app --host 127.0.0.1 --port 5000
   ```

2. Run the MCP server. It needs **no third-party packages** — only `python3`:

   ```sh
   WHOOGLE_URL=http://127.0.0.1:5000 \
   python3 mcp/whoogle_mcp_server.py
   ```

   It speaks MCP over **stdio** (newline-delimited JSON-RPC 2.0 on
   stdin/stdout) and binds no socket of its own.

---

## Tools

### `search(query, limit?)`

Calls the Whoogle JSON endpoint and returns structured results.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `query` | string | yes | The search query. |
| `limit` | integer | no | Max results (default 10, hard-capped at `WHOOGLE_MCP_MAX_RESULTS`, default 20). |

The tool returns a text block for the model *and* a `structuredContent`
object:

```json
{
  "query": "raspberry pi 5",
  "count": 10,
  "total_available": 10,
  "results": [
    {"title": "Raspberry Pi 5", "url": "https://www.raspberrypi.com/products/raspberry-pi-5/", "snippet": "Raspberry Pi 5 features ..."}
  ]
}
```

Note: the backend cannot bound its result count yet (issue #6 adds a `limit`
parameter), so `limit` is applied **client-side** here.

### `fetch_page(url, max_chars?)`

Fetches a URL (typically one returned by `search`) and returns its visible
text, so an agent can read a result without leaving the tool loop. It is
**deliberately not an open proxy** — see [Security](#security) below.

---

## Contract tolerance (important)

The agent-facing contract for the Whoogle JSON endpoint is being hardened
separately in **issue #6**, which standardises on `url` / `snippet` field
names. The **current** backend emits different names:

| Concept | Current backend (`app/routes.py`, `test/test_json.py`) | Issue-#6 target |
|---|---|---|
| target URL | `href` | `url` |
| snippet | `content` (fallback `text`) | `snippet` |
| title | `title` (fallback `text`) | `title` |

This MCP server therefore reads **both** spellings and requires **no field
beyond** title / url / snippet:

* `url`  ← `url` \| `href` \| `link`
* `title` ← `title` \| `text`
* `snippet` ← `snippet` \| `content` \| `description` \| `text`

Unknown extra fields are ignored. When #6 lands and the backend starts
emitting `url`/`snippet`, this server keeps working unchanged. It never
invents a value: a missing snippet is returned as `""`.

If the backend returns something that is **not** a JSON object with a
top-level `results` list, the tool returns an explicit error naming the
actual top-level keys — it does not silently guess.

---

## Security

* **Transport**: stdio only. The server listens on no port and exposes no
  network surface. It makes *outbound* HTTP calls only.
* **`fetch_page` restrictions** (so it cannot be turned into an open proxy):
  * `http` / `https` schemes only;
  * **SSRF guard**: the host is resolved and refused if it maps to a
    loopback, private, link-local, reserved, multicast or unspecified
    address, and the guard is re-applied on **every redirect hop**;
  * only text-ish content types are accepted (`text/*`,
    `application/xhtml+xml`, `application/json`);
  * hard **timeout** (`WHOOGLE_MCP_FETCH_TIMEOUT`, default 10 s) and hard
    **byte cap** (`WHOOGLE_MCP_FETCH_MAX_BYTES`, default 512 KiB);
  * extracted text is capped at `WHOOGLE_MCP_FETCH_MAX_CHARS` (default 4000).
* It does **not** route `fetch_page` through the Whoogle instance (issue #7
  suggested doing so for privacy). Whoogle exposes no clean "fetch this URL
  as text" endpoint, and its `/window` view returns rewritten HTML, not text.
  Fetching directly with the guards above was judged the simpler, more honest
  option; this deviation is called out explicitly rather than hidden.

---

## Configuration

All via environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `WHOOGLE_URL` | `http://127.0.0.1:5000` | Base URL of the Whoogle instance. |
| `WHOOGLE_TIMEOUT` | `30` | Search request timeout (seconds). |
| `WHOOGLE_MCP_MAX_RESULTS` | `20` | Hard cap on results per search. |
| `WHOOGLE_MCP_FETCH_MAX_BYTES` | `524288` | Max bytes read by `fetch_page`. |
| `WHOOGLE_MCP_FETCH_TIMEOUT` | `10` | `fetch_page` timeout (seconds). |
| `WHOOGLE_MCP_FETCH_MAX_CHARS` | `4000` | Max chars of extracted text. |

---

## Registering with an MCP host

The server is a plain MCP stdio server. Any MCP-capable host registers it with
a *command + args + env* triple. The exact host config below is illustrative —
**do not commit it to this repo**; add it in the agent host's own settings.

Example for a generic host (e.g. Claude Desktop `mcpServers`, or an
equivalent `mcp.json`):

```json
{
  "mcpServers": {
    "whoogle": {
      "command": "python3",
      "args": ["/absolute/path/to/whoogle-search/mcp/whoogle_mcp_server.py"],
      "env": {
        "WHOOGLE_URL": "http://127.0.0.1:5000"
      }
    }
  }
}
```

* `command` should be the absolute path to a `python3` (or a venv python).
* `args[0]` must be the **absolute** path to `whoogle_mcp_server.py`.
* Register the instance's real base URL in `WHOOGLE_URL`, and optionally the
  tuning variables from the table above.
* Note: registering a new MCP server with a host generally takes effect on
  **host restart**, not immediately (as issue #7 notes).

---

## Testing / proof

`test/conformance_client.py` drives this server with the **official MCP
Python SDK client** (`mcp`), performing a real `initialize` → `tools/list` →
`tools/call` handshake over stdio against a running Whoogle instance:

```sh
python3 -m venv /tmp/wgmcp-venv && /tmp/wgmcp-venv/bin/pip install -r mcp/requirements-dev.txt
WHOOGLE_URL=http://127.0.0.1:5000 \
  /tmp/wgmcp-venv/bin/python mcp/test/conformance_client.py "raspberry pi 5"
```

The server itself imports **only the standard library** — see
`requirements.txt`.
