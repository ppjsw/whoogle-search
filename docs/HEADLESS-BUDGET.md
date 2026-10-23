# Headless lane: concurrency, cache and timeout budget

The `google-headless` fetch backend (`WHOOGLE_FETCH_BACKEND=google-headless`)
renders a real SERP in one headless Chromium process. On the reference host —
a Raspberry Pi 5 (8 GB) that **also serves house DNS** — a render costs 6–8 s
and a SERP needs one network hop per result to unwrap Google's
`/goto?url=…` tokens. This document states every budget in that path, what
happens when one is exceeded, and the measurements behind the defaults.

- Implementation: `app/services/headless_browser.py` (the driver and the lock),
  `app/services/result_cache.py` (the TTL cache),
  `app/services/fetch_backend.py` (`GoogleHeadlessBackend`: cache lookup, the
  resolution budget, mapping failures onto the JSON contract).
- Contract: [JSON-API.md](JSON-API.md). A budget that expires is reported as
  `error_type: "timeout"` / HTTP 504 — never as "no results".

## The model

One Chromium process, one page target, **one render at a time**. Renders are
serialised by a single lock, so the heavy work on the box is concurrency 1 no
matter how many Flask threads (waitress: 4) are serving requests. A request
that cannot get the lock within its budget fails with a clear timeout instead
of queueing behind a wedged render for ever.

This is deliberate: the instance is **private and single-user** (see the
project charter on issue #1), Google blocking the egress IP is explicitly not a
concern, and the bar is "does not disturb the host", not "survives a crowd".
There is therefore no proxy rotation, no user-agent rotation and no burst
survival machinery — only the guarantees below.

## Knobs

| Knob | Env var | Default | Read | Meaning |
| --- | --- | --- | --- | --- |
| Render timeout | `WHOOGLE_HEADLESS_TIMEOUT` | `30` s | browser start | Wall-clock budget for one navigation + render. On expiry the page target is restarted (`_restart()`). |
| Settle timeout | *(constant)* `DEFAULT_SETTLE_TIMEOUT` | `8` s | — | How long to keep polling for result anchors after the load event before taking whatever the DOM has (so a zero-result query does not burn the full render budget). |
| Lock-acquisition timeout | `WHOOGLE_HEADLESS_LOCK_TIMEOUT` | `45` s | browser start | How long a request waits for the render lock. On expiry: `HeadlessBusy` → HTTP 504 `timeout`. |
| Cache TTL | `WHOOGLE_CACHE_TTL` | `3600` s | first cache use | How long a cached SERP may be served. **`0` disables the cache.** |
| Cache size cap | `WHOOGLE_CACHE_MAX_ENTRIES` | `128` | first cache use | Maximum entries; the oldest inserted entry is evicted first. |
| Resolution budget | `WHOOGLE_HEADLESS_RESOLVE_TIMEOUT` | `20` s | every fetch | Overall budget for the whole `/goto` resolution phase. When it expires the remaining results are dropped. |
| Per-hop timeout | *(constant)* `DEFAULT_RESOLVE_HOP_TIMEOUT` | `5` s | every fetch | Cap on a single `/goto` hop, always capped further by the remaining budget, so one slow hop cannot consume the phase. |

Notes:

- The cache env vars and the browser env vars are read **once** (the cache
  singleton and the browser singleton are created on first use). Changing them
  needs an app restart. The resolution budget is read per fetch.
- `WHOOGLE_HEADLESS_TIMEOUT`, `WHOOGLE_HEADLESS_UA`,
  `WHOOGLE_HEADLESS_CHROMIUM` and `WHOOGLE_HEADLESS_PROFILE_DIR` predate this
  document and keep their behaviour.
- Invalid or non-positive values fall back to the default with a warning
  printed on stdout, so a typo cannot wedge the lane.

## What happens when a budget is exceeded

| Situation | Backend result | JSON contract |
| --- | --- | --- |
| Lock not acquired within `WHOOGLE_HEADLESS_LOCK_TIMEOUT` (wedged render holds it) | `HeadlessBusy` → status `504` | `504`, `error_type: "timeout"` |
| Render exceeds `WHOOGLE_HEADLESS_TIMEOUT` | `HeadlessUnavailable` → status `503` | `502`, `error_type: "upstream_error"` |
| Google serves `/sorry/` | status `429` | `503`, `error_type: "blocked"` |
| Resolution budget expires, at least one result already resolved | status `200`, the resolved results | `200`, `count` is the number served, **no** error key |
| Resolution budget expires with none of the candidates resolved | status `504` | `504`, `error_type: "timeout"` |
| Genuine zero-result SERP | status `200`, empty result set | `200`, `count: 0`, no error key |

A partially resolved SERP is represented honestly as **fewer results**, not as
an error: the links that are present are real, absolute, verified URLs, and
`count` says how many there are. The one case that is not allowed to look like
"no results" is a SERP whose candidates all existed but none could be resolved;
that is a timeout.

## The cache

**What is stored.** The *post-parse* Google-shaped DOM — i.e. the same string
the rest of Whoogle filters and renders — keyed by the full request URL
(`search_url + query`, including `hl`, `gl`, `safe`, `start`, `tbm`, …). A hit
therefore skips **both** the 6–8 s render **and** the `/goto` resolution hops.

**What is never stored.**

- a challenge page (`/sorry/`) — caching a wall would turn a transient block
  into a long-lived wrong answer;
- an unavailable or busy browser;
- an empty result set;
- a SERP whose resolution phase was cut short by the budget (so the next
  request can retry the remaining hops);
- a SERP where *none* of the candidates resolved.

The cache lives in the fetch layer, not in the Flask route, so the HTML view
and the JSON view share it and cannot disagree (issue #6).

**Bounded and thread-safe.** A `threading.Lock` guards every read and write;
entries are evicted oldest-inserted-first once `WHOOGLE_CACHE_MAX_ENTRIES` is
reached; expiry uses `time.monotonic()`, so an NTP step or DST change cannot
resurrect an expired entry. Expired entries are dropped on read.

### Caching resolved URLs: the decision and its risk

The cached value is the **normalised DOM with resolved absolute URLs**, not the
raw rendered HTML. The reasoning:

- After resolution the DOM no longer contains any `/goto` token; every `href`
  is a real destination URL. Serving a cached SERP therefore never asks the
  browser (or the network) to re-resolve anything, so **the lifetime of
  Google's `/goto` tokens is irrelevant** to a cache hit.
- The opposite choice — caching the *raw* HTML — would be the risky one: it
  would force a re-resolution of tokens that may have expired, and would also
  preserve the 8–9 serial network hops on every hit.
- The residual risk is **staleness, not broken links**: a hit may serve a SERP
  up to `WHOOGLE_CACHE_TTL` old (ranking and snippets drift). On a
  private single-user instance that is an acceptable trade for skipping the
  render; `WHOOGLE_CACHE_TTL=0` disables caching entirely if freshness matters
  more, and any smaller value trades freshness for render cost linearly.
- The cache is in-process (not shared between workers) and is lost on restart.
  That is intentional: one process owns one browser, so one cache is enough.

## Measured behaviour

Host: Raspberry Pi 5, 8 GB, 4 cores, also serving house DNS. Driven with
`WHOOGLE_FETCH_BACKEND=google-headless` against a local app on
`127.0.0.1:8899`, using `curl` and a small threaded client.

### The render cost had to be simulated today — read this first

Chromium on this host **currently cannot complete any network navigation**,
independently of this change: a bare `chromium --headless=new --dump-dom`
against `https://example.com/`, `https://1.1.1.1/`, a LAN IP, and even
`http://127.0.0.1:8898/` (a local static server that answers `curl` with 200 in
milliseconds) all hang until the timeout, while `data:` URLs render fine and
`curl` to the same hosts answers in ~0.1 s. A single headless Chromium was
running at the time and the box was otherwise quiet (load average 0.1,
4.3 GB available), so this is not contention. The numbers below therefore
replace **only** the rendered DOM (a canned 9-result SERP) and the `/goto` hop
(a 0.12 s sleep) with fakes; the Flask route, the fetch backend, the TTL cache,
the render lock and its timeout, and the resolution budget are the real code.
Genuine end-to-end render timings on this host are **unverified** — see the PR.

### Cache

| Scenario | HTTP | Latency | Results |
| --- | --- | --- | --- |
| Cold render + resolution (9 results × 0.12 s hop) | 200 | **8.132 s** | 9 |
| Same query again → cache hit | 200 | **0.033 s** | 9 |
| Different query (warm render) | 200 | 8.123 s | 9 |
| Same query again → cache hit | 200 | **0.041 s** | 9 |

A hit is ~250× faster than a render and skips the 1.08 s of resolution hops
too.

### Burst of 5 concurrent requests (2 identical + 3 distinct)

| Request | Latency |
| --- | --- |
| 1 | 8.12 s |
| 2 | 15.13 s |
| 3 | 22.12 s |
| 4 | 29.14 s |
| 5 | 36.14 s |

Wall clock 36.137 s. The requests are strictly serialised (concurrency 1), each
waiting for the previous render — exactly the intended model, no overlapping
Chromium work. Host health during the burst: a DNS lookup against the local
resolver answered in **0.014 s** (2 answers), load average went from
`0.10 0.17 0.23` to `0.06 0.15 0.22`, and available memory stayed at **4.31 GB**
before and after.

### Lock-timeout (GAP 2)

`WHOOGLE_HEADLESS_LOCK_TIMEOUT=1`, render 6 s, three concurrent distinct
queries: one request held the lock and answered **200 in 7.138 s**; the other
two gave up after **1.049 s** and **1.051 s** with

```json
{"count":0,"error":true,"error_message":"The search engine did not answer within the configured budget.","error_type":"timeout","query":"cap two","results":[]}
```

Before this change those two requests would have blocked for the full render,
and behind a wedged render they would have blocked for ever.

### Resolution budget (GAP 3)

Nine results, 2 s per `/goto` hop (deliberately slow), render 7 s:

| Resolution budget | Latency | Results served |
| --- | --- | --- |
| `WHOOGLE_HEADLESS_RESOLVE_TIMEOUT=3` | **11.044 s** | 2 of 9 (rest dropped) |
| default `20` s | 25.051 s | 9 of 9 |

With the default budget, the second identical request was a cache hit
(0.045 s). With the 3 s budget the partial SERP is intentionally *not* cached,
so a repeat costs another 11.042 s — the trade-off is explicit: a bounded but
incomplete answer now, a complete one on the next attempt.

## Known limitations

- On HTTP 504 the **HTML** view renders an empty results page (the JSON view
  is the authoritative one and reports `timeout`). The kiosk view has no error
  banner for this case yet.
- `shutdown()` still waits for an in-flight render (unchanged behaviour); a
  wedged render therefore delays a clean SIGTERM. The browser is still killed
  by `PR_SET_PDEATHSIG` if the app is killed outright.
- The cache is keyed on the exact request URL, so a different `hl`/`gl`/`safe`
  combination is a different cache entry. That is intended (the DOM differs),
  but it means the effective hit rate depends on a stable configuration.
