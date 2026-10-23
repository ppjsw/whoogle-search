"""Persistent headless-Chromium driver for the ``google-headless`` backend.

Google stopped serving organic results to non-JavaScript clients, so the only
way to obtain a real SERP is to *execute* the search page in a real browser
context. This module owns exactly one headless Chromium process, started
lazily on first use and reused for every subsequent query (verified on this
host: cold ~8 s, warm ~6 s), driven over the Chrome DevTools Protocol.

Design constraints (see the project charter on issue #1 - private, single
user, self-hosted):

* **One browser, one page.** A module-level singleton plus a lock guarantees
  concurrency 1: renders never overlap, so at most one heavy render runs at a
  time on the shared 8 GB Pi.
* **Never the desktop browser.** Chromium is always launched ``--headless``
  with its own dedicated ``--user-data-dir``; the owner's interactive session
  is never touched.
* **No leaked processes or profile dirs.** The browser is terminated and its
  profile removed by :func:`shutdown_browser`, registered via :mod:`atexit`
  and a ``SIGTERM`` handler, and the child is also SIGKILLed by the kernel if
  the parent dies unexpectedly (``PR_SET_PDEATHSIG``).

Nothing here parses results - :class:`~app.services.fetch_backend.
GoogleHeadlessBackend` does that. This module only turns a URL into rendered
HTML and resolves Google's ``/goto`` redirect tokens.
"""

import atexit
import ctypes
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import websocket  # provided by the `websocket-client` package
    _WEBSOCKET_IMPORT_ERROR = None
except ImportError as exc:  # pragma: no cover - exercised only without the dep
    websocket = None
    _WEBSOCKET_IMPORT_ERROR = exc


# A genuine desktop Chrome user agent. Google keys its "unusual traffic" wall
# off this string: a default headless UA is served the /sorry/ reCAPTCHA page,
# whereas a real Chrome string is served a normal SERP (verified from this
# host). Version matches the Chromium binary installed on this machine.
DEFAULT_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)

# Total wall-clock budget for one navigation+render, and how long to keep
# polling for rendered results after the load event before giving up (so a
# zero-result or challenge page does not stall the full render timeout).
DEFAULT_RENDER_TIMEOUT = 30.0
DEFAULT_SETTLE_TIMEOUT = 8.0

_CHALLENGE_TITLE_RE = re.compile(r"<title>\s*Sorry\b", re.I)
_CHALLENGE_TEXT_MARKERS = (
    "unusual traffic",
    "g-recaptcha",
    'id="captcha-form"',
)


def looks_like_challenge(html: str, anchor_count: int) -> bool:
    """Decide whether a rendered page is Google's anti-bot wall.

    A normal SERP's own bootstrap JavaScript *contains* the string
    ``/sorry/`` (in ``indexOf("/sorry/index")``), so that alone is useless as
    a marker. The reliable signal is a page with **no result anchors** whose
    title/text carry the challenge wording.
    """
    if anchor_count > 0:
        return False
    if not html:
        return False
    if _CHALLENGE_TITLE_RE.search(html):
        return True
    return any(m in html for m in _CHALLENGE_TEXT_MARKERS)


class HeadlessUnavailable(RuntimeError):
    """Raised when the headless browser cannot be started or driven."""


class HeadlessChallenge(RuntimeError):
    """Raised when Google serves its anti-bot challenge instead of results."""


def _find_chromium() -> str:
    """Locate a Chromium/Chrome binary without downloading anything."""
    env = os.environ.get("WHOOGLE_HEADLESS_CHROMIUM", "").strip()
    if env:
        return env
    for name in ("chromium", "chromium-browser", "google-chrome",
                 "google-chrome-stable", "chrome"):
        path = shutil.which(name)
        if path:
            return path
    return ""


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _enable_pdeathsig() -> None:
    """Ask the kernel to SIGKILL this child if its parent dies.

    Belt-and-braces against the app being SIGKILLed (atexit would not run):
    the Chromium process can never be orphaned.
    """
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        PR_SET_PDEATHSIG = 1
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
    except Exception:
        pass


class _CDP:
    """Minimal Chrome DevTools Protocol client over one websocket."""

    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, max_size=None)
        self._id = 0

    def send(self, method: str, timeout: float = 30.0, **params):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps(
            {"id": mid, "method": method, "params": params}))
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(method)
            self.ws.settimeout(remaining)
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})

    def wait_event(self, name: str, timeout: float) -> bool:
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            self.ws.settimeout(remaining)
            try:
                msg = json.loads(self.ws.recv())
            except Exception:
                return False
            if msg.get("method") == name:
                return True

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


class HeadlessBrowser:
    """A single reusable headless Chromium process + page target."""

    def __init__(self,
                 user_agent: str = DEFAULT_BROWSER_UA,
                 chromium: str = "",
                 profile_dir: str = "",
                 render_timeout: float = DEFAULT_RENDER_TIMEOUT,
                 settle_timeout: float = DEFAULT_SETTLE_TIMEOUT):
        self.user_agent = user_agent
        self.chromium = chromium or _find_chromium()
        self._explicit_profile = profile_dir
        self.profile_dir = profile_dir
        self.render_timeout = render_timeout
        self.settle_timeout = settle_timeout

        self._proc = None
        self._cdp = None
        self._port = None
        self._lock = threading.Lock()   # concurrency 1

    # -- lifecycle ---------------------------------------------------------
    def _start(self) -> None:
        if not self.chromium or not os.path.exists(self.chromium):
            raise HeadlessUnavailable(
                "no Chromium binary found (set WHOOGLE_HEADLESS_CHROMIUM)")
        if websocket is None:
            raise HeadlessUnavailable(
                "websocket-client is required: %s" % _WEBSOCKET_IMPORT_ERROR)

        if not self.profile_dir:
            self.profile_dir = tempfile.mkdtemp(prefix="whoogle-headless-")
        os.makedirs(self.profile_dir, exist_ok=True)
        self._port = _free_port()

        cmd = [
            self.chromium,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-extensions",
            "--mute-audio",
            # Never talk to the desktop keyring. With a D-Bus session bus
            # reachable and the login keyring locked (gnome-keyring waiting on
            # an unlock prompt), Chromium blocks forever during password-store
            # initialisation: it opens a TCP connection to the target but never
            # writes the HTTP request, so every render times out. Verified on
            # this host - the same binary renders a SERP in ~4 s with this
            # flag (or with DBUS_SESSION_BUS_ADDRESS unset) and hangs
            # indefinitely without it. A headless renderer has no use for the
            # keyring anyway.
            "--password-store=basic",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={self._port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={self.profile_dir}",
            "about:blank",
        ]
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=_enable_pdeathsig)
        self._wait_for_port()
        self._attach_page()

    def _wait_for_port(self) -> None:
        deadline = time.time() + 20.0
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise HeadlessUnavailable(
                    "chromium exited during startup (rc=%s)"
                    % self._proc.returncode)
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self._port}/json/version",
                        timeout=2) as r:
                    r.read()
                    return
            except Exception:
                time.sleep(0.15)
        raise HeadlessUnavailable("chromium DevTools port never opened")

    def _attach_page(self) -> None:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self._port}/json/list", timeout=5) as r:
            targets = json.loads(r.read())
        page = next((t for t in targets if t.get("type") == "page"), None)
        if page is None:
            raise HeadlessUnavailable("no page target in headless chromium")
        self._cdp = _CDP(page["webSocketDebuggerUrl"])
        self._cdp.send("Page.enable")
        self._cdp.send("Runtime.enable")
        self._cdp.send("Network.enable")
        self._cdp.send("Network.setUserAgentOverride",
                       userAgent=self.user_agent)

    def _kill(self) -> None:
        if self._cdp is not None:
            self._cdp.close()
            self._cdp = None
        if self._proc is not None:
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=8)
            except Exception:
                pass
            self._proc = None

    def shutdown(self) -> None:
        """Stop the browser and remove its profile directory."""
        with self._lock:
            self._kill()
            if self.profile_dir and not self._explicit_profile:
                shutil.rmtree(self.profile_dir, ignore_errors=True)
            self.profile_dir = self._explicit_profile

    # -- rendering ---------------------------------------------------------
    def _ensure_started(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            self._kill()
            self._start()

    def _restart(self) -> None:
        """Kill and relaunch after a wedged/timed-out page."""
        self._kill()
        self._start()

    _ANCHOR_JS = "document.querySelectorAll('a h3').length"
    _OUTER_HTML_JS = "document.documentElement.outerHTML"

    def render(self, url: str, timeout: float = None) -> str:
        """Load ``url`` headless and return the rendered DOM as HTML.

        Serialised with a lock (concurrency 1). Raises
        :class:`HeadlessChallenge` if Google serves its anti-bot page.
        """
        timeout = timeout or self.render_timeout
        with self._lock:
            self._ensure_started()
            try:
                return self._render_locked(url, timeout)
            except HeadlessChallenge:
                raise
            except Exception:
                # A wedged page/websocket can poison the single target; a
                # clean restart is cheaper than trying to reason about it.
                self._restart()
                raise

    def _render_locked(self, url: str, timeout: float) -> str:
        deadline = time.time() + timeout
        anchor_count = 0
        try:
            self._cdp.send("Page.navigate", url=url, timeout=timeout)
            self._cdp.wait_event(
                "Page.loadEventFired",
                max(0.5, deadline - time.time()))
            # Poll for rendered results, but only for `settle_timeout`; then
            # take whatever is there (a zero-result query should not stall).
            results_deadline = min(
                deadline, time.time() + self.settle_timeout)
            while time.time() < results_deadline:
                val = self._cdp.send(
                    "Runtime.evaluate",
                    timeout=max(1.0, deadline - time.time()),
                    returnByValue=True, expression=self._ANCHOR_JS)
                anchor_count = val.get("result", {}).get("value", 0) or 0
                if anchor_count:
                    break
                time.sleep(0.25)
            html = self._cdp.send(
                "Runtime.evaluate",
                timeout=max(1.0, deadline - time.time()),
                returnByValue=True,
                expression=self._OUTER_HTML_JS)["result"]["value"]
        except Exception as exc:
            raise HeadlessUnavailable("render failed for %s: %s" % (url, exc))
        if html is None:
            raise HeadlessUnavailable("empty DOM for %s" % url)
        if anchor_count == 0 and looks_like_challenge(html, anchor_count):
            raise HeadlessChallenge(
                "Google served its /sorry/ anti-bot page for %s" % url)
        return html


# -- module-level singleton + token resolution ------------------------------

_browser = None
_browser_lock = threading.Lock()
_atexit_registered = False


def get_browser() -> HeadlessBrowser:
    """Return the process-wide headless browser, starting it on first use."""
    global _browser, _atexit_registered
    with _browser_lock:
        if _browser is None:
            _browser = HeadlessBrowser(
                user_agent=os.environ.get(
                    "WHOOGLE_HEADLESS_UA", DEFAULT_BROWSER_UA),
                chromium=os.environ.get("WHOOGLE_HEADLESS_CHROMIUM", ""),
                profile_dir=os.environ.get(
                    "WHOOGLE_HEADLESS_PROFILE_DIR", ""),
                render_timeout=float(os.environ.get(
                    "WHOOGLE_HEADLESS_TIMEOUT", DEFAULT_RENDER_TIMEOUT)),
            )
        if not _atexit_registered:
            atexit.register(shutdown_browser)
            _register_sigterm(shutdown_browser)
            _atexit_registered = True
        return _browser


def _register_sigterm(handler) -> None:
    """Install a SIGTERM handler that cleans up, only from the main thread."""
    if threading.current_thread() is not threading.main_thread():
        return
    try:
        previous = signal.getsignal(signal.SIGTERM)

        def _handler(signum, frame):
            handler()
            if callable(previous):
                previous(signum, frame)
            else:
                signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum)

        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError):
        pass


def shutdown_browser() -> None:
    """Stop and clean up the singleton browser (idempotent)."""
    global _browser
    with _browser_lock:
        if _browser is not None:
            _browser.shutdown()
            _browser = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_GOOGLE_REDIRECT_RE = re.compile(
    r"^https?://(www\.)?google\.[^/]+/(url|goto)\b")


def resolve_result_url(href: str, user_agent: str = DEFAULT_BROWSER_UA,
                       timeout: float = 15.0):
    """Resolve a Google result href to its real destination URL.

    Current Google SERPs carry opaque ``/goto?url=<token>`` links; a GET on
    the redirect with redirects disabled returns HTTP 302 whose ``Location``
    header is the real target (verified 8/8 from this host, no cookies, no
    JS). The legacy ``/url?q=<url>`` form is still handled if it ever
    reappears, and an already-absolute third-party URL is returned unchanged.

    Returns the resolved absolute URL, or ``None`` if it could not be
    resolved.
    """
    if not href:
        return None
    # Legacy classic form, possibly relative or absolute.
    if "/url?q=" in href:
        q = href.split("/url?q=", 1)[1].split("&", 1)[0]
        return urllib.parse.unquote(q)
    if href.startswith("http"):
        # An ordinary external link is already the destination; only Google's
        # own redirect endpoints need a network hop.
        if not _GOOGLE_REDIRECT_RE.match(href):
            return href
        target = href
    elif href.startswith("/"):
        target = "https://www.google.com" + href
    else:
        return None

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(target, method="GET",
                                 headers={"User-Agent": user_agent})
    try:
        opener.open(req, timeout=timeout)
        return None
    except urllib.error.HTTPError as exc:
        return exc.headers.get("Location")
    except Exception:
        return None
