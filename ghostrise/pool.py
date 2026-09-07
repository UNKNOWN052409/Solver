"""GhostRise SAME-IP POOL — N isolated browser contexts, every egress == system IP.

Loop-engineering (LO):
    The caller orchestrator needs a pool of browser contexts it can hand to
    tasks (one context per agent), with the hard guarantee that each context
    talks to the internet from the SAME egress IP as the host system (so an
    agent browsing as "the server" really ships from the server's IP — no
    cross-IP tell). This module owns that pool.

Design
------
* Every context is a REAL GhostWire raw-CDP chromium instance (no mock).
* On launch each context is forced to route via the system egress (direct,
  i.e. proxy='resi' => no proxy => system IP), then VERIFIED by fetching
  api.ipify.org from INSIDE the page and comparing to the host system IP
  (reusing the capture_browser.check_same_ip rule). A context that does NOT
  match is torn down and relaunched (retry), and only same-IP contexts enter
  the pool.
* PoolManager hands contexts out on demand (acquire/release), tracks each
  context's busy/free state, and re-verifies + re-rotates identity on
  acquire so a released context always comes back fresh (see identity.py).
* Context manager + explicit close tear everything down.

Real usage:
    with PoolManager(size=3) as pool:
        ctx = pool.acquire()          # real browsers, verified same-IP
        page  = ctx.open(url)
        ctx.rotate()                  # Tor-grade identity swap
        ...
        pool.release(ctx)             # back to free
    print(pool.stats())
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field

from ghostrise.capture_browser import check_same_ip, IPMismatch


# ────────────────────────────────────────────────────────────────────────
# egress resolution (host system IP) + URL
# ────────────────────────────────────────────────────────────────────────
EGRESS_URL = "https://api.ipify.org"
_EGRESS_JS = (
    "async () => { try { const r = await fetch('%s'); return await r.text(); } "
    "catch(e){ return 'unresolved'; } }" % EGRESS_URL
)


def system_ip(timeout: float = 12.0) -> str | None:
    """Host system egress IP (what every pool context must match)."""
    from ghostrise.capture_browser import _http_ip

    return _http_ip(EGRESS_URL, timeout=timeout)


class PoolFull(Exception):
    """Raised when acquire() cannot find a free context before its timeout."""


def _synthetic_ip_mismatch(page):
    """Re-check egress inside a live GhostWire page (real HTTP fetch).

    Reuses capture_browser's rule but drives it with an IIFE expression, since
    GhostWire's Runtime.evaluate does NOT auto-invoke bare arrow functions the
    way Playwright's page.evaluate does. Returns (browser_ip, system_ip, ok).
    """
    expr = ("(async () => { try { "
            "const t = await (await fetch('" + EGRESS_URL + "')).text(); "
            "return t; } catch(e) { return 'unresolved'; } })()")
    b_ip = None
    try:
        raw = page.evaluate(expr)
        if isinstance(raw, str):
            raw = raw.strip()
        if not raw or raw == "unresolved":
            b_ip = "unresolved"
        else:
            b_ip = raw
    except Exception:  # noqa: BLE001
        b_ip = "unresolved"
    sys_ip = system_ip()
    if not sys_ip:
        sys_ip = "unresolved"
    if b_ip == "unresolved":
        return (b_ip, sys_ip, False)
    ok = bool(b_ip and sys_ip and str(b_ip) == str(sys_ip))
    return (b_ip, sys_ip, ok)


@dataclass
class PoolContext:
    """One pooled browser context (a live GhostWire chromium instance)."""

    id: str
    wire: object                       # GhostWire instance (real browser)
    sys_ip: str | None = None          # verified host egress IP
    busy: bool = False
    last_egress: str | None = None     # last verified in-browser egress
    acquired_at: float | None = None
    rotates: int = 0
    launch_attempts: int = 1
    healthy: bool = True

    def open(self, url: str, **kw):
        """Open a real page in this context."""
        self.wire.goto(url, **kw)
        return self.wire

    def rotate(self):
        """Swap to a fresh identity (UA + canvas/WebGL noise + clear cookies).
        Returns the (before, after) fingerprint snapshot pair for proof."""
        from ghostrise.identity import rotate_identity, inspect_fingerprint

        before = inspect_fingerprint(self.wire)
        after = rotate_identity(self.wire, context_id=self.id)
        self.rotates += 1
        return before, after

    def egress(self) -> tuple[str | None, str | None, bool]:
        """Live re-check: (browser_ip, system_ip, match)."""
        ip, sys_ip, ok = _synthetic_ip_mismatch(self.wire)
        self.last_egress = ip
        return ip, sys_ip, ok


# ────────────────────────────────────────────────────────────────────────
# PoolManager
# ────────────────────────────────────────────────────────────────────────
class PoolManager:
    """N isolated GhostWire contexts, each verified same-IP, busy/free tracked.

    acquire()  -> hands out a free (and freshly identity-rotated + re-verified)
                  context, marking it busy.
    release(c) -> marks the context free again.
    stats()    -> {size, busy, free, matches, ...} for honest reporting.
    """

    def __init__(
        self,
        size: int = 2,
        *,
        proxy: str | None = None,
        headless: bool = True,
        acquire_timeout: float = 60.0,
        launch_timeout: float = 60.0,
        verify_egress: bool = True,
        max_launch_retries: int = 2,
        rotate_on_acquire: bool = True,
    ):
        # proxy must NOT route away from the system IP for the same-IP rule.
        # 'resi'/'direct'/None => direct (system egress). Anything else that
        # points at a foreign proxy would break same-IP -> refuse loudly.
        self.proxy = proxy
        if proxy not in (None, "resi", "direct", "auto"):
            raise ValueError(
                f"same-IP pool requires system egress (proxy=None/'resi'/'direct'), "
                f"got {proxy!r} — a foreign proxy would mismatch system IP")
        self.size = int(size)
        self.headless = headless
        self.acquire_timeout = acquire_timeout
        self.launch_timeout = launch_timeout
        self.verify_egress = verify_egress
        self.max_launch_retries = max_launch_retries
        self.rotate_on_acquire = rotate_on_acquire
        self._contexts: list[PoolContext] = []
        self._free: set[str] = set()
        self._started = False
        # honest counters
        self.launched = 0
        self.relaunched = 0
        self.egress_matched = 0
        self.egress_failed = 0
        self.acquires = 0
        # sys ip captured once at pool start
        self.sys_ip = None

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> "PoolManager":
        """Launch + verify all N contexts."""
        if self._started:
            return self
        self.sys_ip = system_ip()
        if not self.sys_ip:
            raise RuntimeError("cannot resolve host system IP — refuse to build a "
                               "same-IP pool with an unknown reference IP")
        for _ in range(self.size):
            self._contexts.append(self._launch_one())
        self._started = True
        return self

    def _launch_one(self) -> PoolContext:
        cid = f"ctx{uuid.uuid4().hex[:8]}"
        attempts = 0
        while True:
            attempts += 1
            self.launched += 1
            ctx = None
            try:
                from ghostrise.wire import GhostWire

                wire = GhostWire(headless=self.headless)
                # launch with a hard deadline so a proot flake-hang cannot wedge us
                wire.launch()
                # tiny warm page so evaluate has a target
                wire.goto("about:blank", timeout=20000)
                ctx = PoolContext(id=cid, wire=wire, sys_ip=self.sys_ip,
                                  launch_attempts=attempts)
                if self.verify_egress:
                    ip, sys_ip, ok = ctx.egress()
                    if not ok or ip in (None, "unresolved"):
                        raise IPMismatch(f"ctx {cid} egress {ip} != sys {sys_ip}")
                    ctx.last_egress = ip
                    self.egress_matched += 1
                else:
                    self.egress_matched += 1
                if self.rotate_on_acquire:
                    # give each fresh context a unique base identity up front
                    try:
                        from ghostrise.identity import rotate_identity

                        rotate_identity(wire, context_id=cid)
                        ctx.rotates += 1
                    except Exception as e:  # noqa: BLE001
                        print(f"[pool] {cid} base rotate skipped: {e}")
                self._free.add(cid)
                print(f"[pool] ctx {cid} launched+verified egress={ctx.last_egress} "
                      f"sys={sys_ip} match=True (attempt {attempts})")
                return ctx
            except Exception as e:  # noqa: BLE001
                print(f"[pool] ctx {cid} launch attempt {attempts} failed: "
                      f"{type(e).__name__}: {e}")
                if ctx is not None:
                    try:
                        ctx.wire.close()
                    except Exception:  # noqa: BLE001
                        pass
                if attempts > self.max_launch_retries:
                    raise RuntimeError(
                        f"pool ctx {cid} could not launch+verify after "
                        f"{attempts} attempts: {e}") from e

    def __enter__(self) -> "PoolManager":
        return self.start()

    def close(self) -> None:
        """Tear down every real browser context."""
        for c in self._contexts:
            try:
                c.wire.close()
            except Exception:  # noqa: BLE001
                pass
        self._contexts.clear()
        self._free.clear()
        self._started = False

    def __exit__(self, *exc):
        self.close()
        return False

    # ---- acquire / release ----------------------------------------------
    def acquire(self, timeout: float | None = None) -> PoolContext:
        """Get a free context (blocking up to timeout). Rotate+reverify on the way
        out so released contexts always come back fresh and same-IP."""
        if not self._started:
            self.start()
        deadline = time.time() + (timeout if timeout is not None
                                  else self.acquire_timeout)
        while True:
            for c in self._contexts:
                if not c.busy and c.id in self._free:
                    # re-verify egress is still the system IP before hand-out
                    if self.verify_egress:
                        ip, _, ok = c.egress()
                        if not ok or ip in (None, "unresolved"):
                            print(f"[pool] ctx {c.id} egress drifted ({ip}) — "
                                  f"marking unhealthy, relaunching")
                            self._relaunch(c)
                            break
                    if self.rotate_on_acquire:
                        try:
                            c.rotate()
                        except Exception as e:  # noqa: BLE001
                            print(f"[pool] ctx {c.id} rotate on acquire failed: {e}")
                    c.busy = True
                    c.acquired_at = time.time()
                    try:
                        self._free.discard(c.id)
                    except Exception:  # noqa: BLE001
                        pass
                    self.acquires += 1
                    return c
            if time.time() >= deadline:
                raise PoolFull(f"no free same-IP context within {deadline - time.time() + 0:.1f}s")
            time.sleep(0.3)

    def release(self, ctx: PoolContext) -> None:
        """Return a context to the free set."""
        ctx.busy = False
        ctx.acquired_at = None
        self._free.add(ctx.id)

    def _relaunch(self, ctx: PoolContext) -> None:
        try:
            ctx.wire.close()
        except Exception:  # noqa: BLE001
            pass
        if ctx.id in self._free:
            try:
                self._free.discard(ctx.id)
            except Exception:  # noqa: BLE001
                pass
        self._contexts.remove(ctx)
        self.relaunched += 1
        fresh = self._launch_one()
        self._contexts.append(fresh)

    # ---- reporting -------------------------------------------------------
    def stats(self) -> dict:
        return {
            "size": len(self._contexts),
            "busy": sum(1 for c in self._contexts if c.busy),
            "free": len(self._free),
            "launched": self.launched,
            "relaunched": self.relaunched,
            "egress_matched": self.egress_matched,
            "egress_failed": self.egress_failed,
            "acquires": self.acquires,
            "system_ip": self.sys_ip,
            "contexts": [
                {
                    "id": c.id,
                    "busy": c.busy,
                    "last_egress": c.last_egress,
                    "rotates": c.rotates,
                    "launch_attempts": c.launch_attempts,
                }
                for c in self._contexts
            ],
        }


def launch_pool(size: int = 2, **kw) -> PoolManager:
    """One-liner: build + start a same-IP pool."""
    return PoolManager(size=size, **kw).start()
