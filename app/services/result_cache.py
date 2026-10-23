"""A small in-process TTL cache for the ``google-headless`` fetch lane.

Rendering a SERP in headless Chromium costs 6-8 s on the reference host (a
Raspberry Pi 5), so an identical query issued twice should not pay that cost
twice. This module is deliberately tiny and dependency-free:

* **TTL** (``WHOOGLE_CACHE_TTL``, default 3600 s). ``0`` disables the cache
  entirely. A generous TTL is correct for this project: it is a *private,
  single-user* instance (see the charter on issue #1), so absorbing the render
  cost matters far more than seeing the very latest SERP.
* **Bounded size** (``WHOOGLE_CACHE_MAX_ENTRIES``, default 128). A
  long-running instance must not grow without bound, so the oldest inserted
  entry is evicted first once the cap is reached.
* **Thread-safe**. Flask serves requests concurrently, so every read and write
  takes a lock.

The cache stores a *value* only - it knows nothing about HTTP or HTML. The
caller (:class:`app.services.fetch_backend.GoogleHeadlessBackend`) is
responsible for only inserting values that are safe to serve again; in
particular a challenge page or an empty result set must never be cached, or a
transient block would become a long-lived wrong answer.

The env vars are read once, when the process-wide instance is first created,
so changing them requires an app restart (documented in
``docs/HEADLESS-BUDGET.md``).
"""

import os
import threading
import time
from collections import OrderedDict

# One hour: long enough to absorb the render cost across a working session,
# short enough that a repeated query eventually sees fresh results.
DEFAULT_TTL = 3600.0

# Bounded: 128 SERPs of a few hundred KB is tens of MB at worst, and the cap
# stops a long-running instance from growing without bound.
DEFAULT_MAX_ENTRIES = 128


def _env_number(name, default, cast):
    """Read a numeric env var, falling back to ``default`` when unusable."""
    raw = os.environ.get(name, "")
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return cast(str(raw).strip())
    except (TypeError, ValueError):
        print("Warning: ignoring invalid %s=%r (using %r)"
              % (name, raw, default))
        return default


class ResultCache:
    """A fixed-capacity, TTL-expiring mapping of request URL -> result DOM."""

    def __init__(self, ttl=DEFAULT_TTL, max_entries=DEFAULT_MAX_ENTRIES,
                 clock=None):
        self.ttl = max(0.0, float(ttl))
        self.max_entries = max(0, int(max_entries))
        # Monotonic so a wall-clock jump (NTP step, DST) cannot resurrect an
        # expired entry or expire a fresh one.
        self._clock = clock or time.monotonic
        self._entries = OrderedDict()   # key -> (expires_at, value)
        self._lock = threading.Lock()

    @property
    def enabled(self):
        """False when caching is switched off (TTL 0 or no capacity)."""
        return self.ttl > 0 and self.max_entries > 0

    def get(self, key):
        """Return the live value for ``key``, or ``None`` on miss/expiry."""
        if not self.enabled:
            return None
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at <= self._clock():
                del self._entries[key]
                return None
            return value

    def set(self, key, value):
        """Store ``value`` for ``key``, evicting the oldest entry if full."""
        if not self.enabled or value is None:
            return
        with self._lock:
            # Re-inserting moves the key to the newest position, so a key that
            # is refreshed counts as recent rather than stale.
            self._entries.pop(key, None)
            self._entries[key] = (self._clock() + self.ttl, value)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def clear(self):
        """Drop every entry (used by tests and by an explicit refresh)."""
        with self._lock:
            self._entries.clear()

    def __len__(self):
        with self._lock:
            return len(self._entries)


def from_env():
    """Build a cache from ``WHOOGLE_CACHE_TTL`` / ``WHOOGLE_CACHE_MAX_ENTRIES``."""
    return ResultCache(
        ttl=_env_number("WHOOGLE_CACHE_TTL", DEFAULT_TTL, float),
        max_entries=_env_number(
            "WHOOGLE_CACHE_MAX_ENTRIES", DEFAULT_MAX_ENTRIES, int),
    )


# -- process-wide singleton --------------------------------------------------

_cache = None
_cache_lock = threading.Lock()


def get_result_cache():
    """Return the process-wide cache, creating it from the env on first use."""
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = from_env()
        return _cache


def reset_result_cache(cache=None):
    """Replace the singleton (tests, or a deliberate flush).

    ``get_result_cache()`` rebuilds it from the environment on next use.
    """
    global _cache
    with _cache_lock:
        _cache = cache
