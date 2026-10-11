# JSON results API

The `/search` endpoint can return its filtered results as JSON. This is the
**agent-facing contract**: a stable, documented shape an AI agent (or any
program) can depend on, produced by the *same* fetch/parse/filter layer as the
HTML view so the two can never disagree about which results exist.

- Implementation: `app/routes.py` (`search()`) + `app/utils/json_api.py`.
- The JSON path is not a side door bolted onto the HTML path — both are rendered
  from the one post-`Filter` soup (`Search.generate_response()`).

## Requesting JSON

Either form works:

| Form | Example |
| --- | --- |
| Query parameter | `GET /search?q=raspberry+pi+5&format=json` |
| Content negotiation | `GET /search?q=raspberry+pi+5` with header `Accept: application/json` |

Optional parameter:

| Parameter | Type | Meaning |
| --- | --- | --- |
| `limit` | positive integer | Caps the number of returned results. |

`limit` can only **reduce** the result count below what the engine returned; see
[Honest limits](#honest-limits). A non-integer or non-positive `limit` is a
`400 invalid_request` (below).

## Success response (HTTP 200)

```json
{
  "query": "raspberry pi 5",
  "search_type": "",
  "count": 9,
  "results": [
    {
      "position": 1,
      "title": "Buy a Raspberry Pi 5",
      "href": "https://www.raspberrypi.com/products/raspberry-pi-5/",
      "snippet": "Raspberry Pi 5 features the Broadcom BCM2712 quad-core Arm Cortex A76 processor @ 2.4GHz, making it up to three times faster than the previous generation. With ...",
      "content": "Raspberry Pi 5 features the Broadcom BCM2712 quad-core Arm Cortex A76 processor @ 2.4GHz, making it up to three times faster than the previous generation. With ...",
      "text": "Buy a Raspberry Pi 5 Raspberry Pi 5 features the Broadcom BCM2712 quad-core Arm Cortex A76 processor @ 2.4GHz, making it up to three times faster than the previous generation. With ..."
    }
    /* ... 8 more results ... */
  ]
}
```

(The example above is an abridged excerpt of a live `google-headless` response
captured on this host: the `count` is 9 and the first result is reproduced
verbatim; 8 further results are elided by the `/* ... */` note. See the PR for
the full raw body.)

### Envelope fields

| Field | Type | Notes |
| --- | --- | --- |
| `query` | string | The decoded query. |
| `search_type` | string | `""` for web, else the `tbm` value used. |
| `count` | integer | `len(results)` — the number of results in this response. |
| `results` | array | The result objects below. Always present (empty array on no results). |

### Result fields

| Field | Type | Notes |
| --- | --- | --- |
| `position` | integer | 1-based rank within this page. |
| `title` | string | Result title. |
| `href` | string | **Fully resolved absolute real URL.** Never a Google `/url?q=` or `/goto?url=<token>` token, never a DuckDuckGo `/l/?uddg=` wrapper. See [URL guarantee](#url-guarantee). |
| `snippet` | string | The description text. May be `""`. |
| `content` | string | Legacy alias of `snippet`; carries the identical value. |
| `text` | string | Legacy convenience field: the full result-container text (often title+snippet concatenated). |

`title`, `href` and `text`/`content` predate this contract and are retained for
backwards compatibility (other consumers, including the planned MCP server,
depend on them). `position`, `count` and `snippet` are added by this contract.

## No results (HTTP 200, not an error)

A genuine empty result set is a **success**: HTTP 200 with `count: 0` and
`results: []`, and **no** `error` or `blocked` key. A caller can therefore
distinguish "the engine found nothing" from "the engine blocked us" by the
presence of `error`/`blocked` and the HTTP status alone.

```json
{"count":0,"query":"zxqjwvbnmplkjhgfdsa092837465zzzqqxx","results":[],"search_type":""}
```

## Error shapes

Every error response is JSON, carries a machine-readable `error_type`, a
human-readable `error_message`, and an **empty `results` array with `count: 0`**
— so a caller can always read `.results` without branching on key presence.

| `error_type` | HTTP | Meaning |
| --- | --- | --- |
| `blocked` | 503 | The engine served an anti-bot / CAPTCHA challenge page. |
| `timeout` | 504 | The upstream engine timed out. |
| `upstream_error` | 502 | The engine was unreachable / returned no usable response. |
| `invalid_request` | 400 | A bad request parameter (e.g. non-integer `limit`). |
| `cse_error` / `cse_quota` | (CSE code) | Custom Search API error (only when CSE is enabled). |
| `internal_error` | 500 | Unexpected server error. |

`blocked` additionally sets the legacy `"blocked": true` key. All errors also
set the legacy `"error": true`.

### blocked (503) — challenged, NOT no-results

```json
{"blocked":true,"count":0,"error":true,"error_message":"Instance has been ratelimited","error_type":"blocked","query":"raspberry pi 5","results":[]}
```

This is the issue #9 requirement made concrete: the block above and the
no-results response further up share an empty `results` array, but only the
block carries `error_type: "blocked"`, `blocked: true`, and HTTP 503.

### upstream_error (502)

```json
{"count":0,"error":true,"error_message":"Upstream search engine returned no usable response.","error_type":"upstream_error","query":"raspberry pi 5","results":[]}
```

### invalid_request (400)

```json
{"count":0,"error":true,"error_message":"'limit' must be a positive integer","error_type":"invalid_request","query":"raspberry pi 5","results":[]}
```

### timeout (504)

```json
{"count":0,"error":true,"error_message":"Upstream search engine timed out.","error_type":"timeout","query":"raspberry pi 5","results":[]}
```

### Feeling Lucky (HTTP 303)

A query containing the "feeling lucky" marker returns HTTP 303 with:

```json
{"redirect": "https://example.com/lucky"}
```

This is unchanged from before this contract.

## URL guarantee

`results[].href` is enforced by `app/utils/json_api.py` (`_safe_href` /
`is_redirect_token`):

- A Google `https://www.google.com/url?q=<url>` wrapper is unwrapped to `<url>`.
- A DuckDuckGo `//duckduckgo.com/l/?uddg=<url>` wrapper is unwrapped.
- A Google `https://www.google.com/goto?url=<opaque token>` cannot be resolved
  without a network hop (the token is not a URL), so that result is **dropped**
  rather than emitted as a token. (The `google-headless` backend resolves
  `/goto` tokens at fetch time, so this is a safety net, not the normal path.)
- Relative and non-`http(s)` links are dropped.

Note: a resolved real URL may still legitimately carry an engine tracking query
parameter (e.g. `srsltid=...` on an otherwise real `price.com.hk` URL). The
guarantee is about redirect *tokens*, not tracking parameters.

## Honest limits

- **One page only.** Google now serves only 8–9 natural results per SERP (the
  rest of the page is AI-overview / people-also-ask widgets). `limit` above ~9
  is accepted but cannot be satisfied by a single page: you get at most what the
  engine returned. There is no pagination in the JSON contract.
- **Result count is not guaranteed.** 8–9 is what Google served on this host for
  the observed queries, not a fixed number.
- **Block detection signal.** For `google-headless` the block is reported by the
  backend as an upstream HTTP 429 (the browser sees a `/sorry/` anti-bot page);
  for `ddg-html` as an upstream HTTP 202 with no result blocks. In addition an
  empty page is classified `blocked` when an anti-bot signature appears in the
  **raw, un-filtered upstream body** — matched case-insensitively and
  quote-agnostically: Google's `retry/enablejs` interstitial, `captcha-form`,
  `g-recaptcha`, `unusual traffic`, `/sorry/index`,
  `support.google.com/sorry`, or a `<title>Sorry…`. The raw body is used
  because the `Filter` strips that markup from the rendered output. Recall is
  deliberately favoured over precision: reporting `blocked` for a page that
  merely mentions one of these phrases is the safe direction for a caller
  (it can retry), whereas silently returning an empty result set misleads. The
  net is still **best-effort, not a guarantee**: the (dead) default
  `google-html` backend, queried live with this instance's User-Agent pool,
  returned a Google WAP/unsupported-browser page (no results, no recognised
  signature), which is reported as an empty result set. The reliable signal is
  the upstream status from the `google-headless` / `ddg-html` backends. The
  result-block precondition is parsed, not substring-matched. See the PR's RISK
  section: the `google-headless` lane's own detection is User-Agent based and
  may tighten.
- **The HTML view is unchanged** by this contract. A challenge still renders an
  (empty) HTML results page there; only the JSON path surfaces the structured
  error. This is deliberate — the requirement in issue #9 is about the
  agent-facing contract.
- **Other backends unchanged.** `google-html` and `ddg-html` keep their existing
  fetch/parse behaviour; only how their (possibly empty) result is *reported* in
  JSON changed.
