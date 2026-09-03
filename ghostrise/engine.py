"""GhostRise - anti-detect browsing engine.

v0.2 rides the CloakBrowser engine: a Chromium build with 49 source-level
C++ patches (canvas, WebGL, fonts, GPU, WebRTC, network timing, automation
signals) exposed through a drop-in Playwright API.

    - headed AND headless from the SAME stealth binary -> no differential
      fingerprint between modes
    - persistent per-profile identities via the profile vault
    - native proxy support; WebRTC exit-IP spoofing + geoip matching
    - humanize=True behavioral layer (Bezier mouse, per-char typing)
    - automatic replay of vaulted cf_clearance for the target
"""

import json
import time
from pathlib import Path
from urllib.parse import urlparse

from ghostrise.profiles import load_profile


def _proxy_url(proxy: str | None) -> str | None:
    """Normalize any common proxy notation to a URL string for cloakbrowser.

    Modes:
      'pool' — solver.proxies healthy proxy auto-pick (LRU rotation)
      'resi' — system ka apna residential IP (direct, no proxy) —
               Jio/Airtel/home-broadband egress Turnstile-mint-friendly
      'auto' — pehle pool; koi healthy proxy nahi to system resi direct
    """
    if not proxy:
        return None
    p = proxy.strip()
    if p in ("resi", "direct"):
        return None                      # direct = system egress
    if p == "pool":
        try:
            from solver.proxies import default_pool
            picked = default_pool().next()
            if picked:
                return picked
            print("[pool] koi healthy proxy nahi — direct ja raha hoon")
            return None
        except Exception as e:
            print(f"[pool] fallback direct: {str(e)[:60]}")
            return None
    if p == "auto":
        try:
            from solver.proxies import default_pool
            picked = default_pool().next()
            if picked:
                return picked
        except Exception:
            pass
        return None                      # system resi direct
    rest = proxy.split("://", 1)[-1]
    if "@" in rest:
        creds, hostport = rest.rsplit("@", 1)
        user, _, password = creds.partition(":")
        return f"http://{user}:{password}@{hostport}"
    parts = rest.split(":")
    if len(parts) == 4:
        host, port, user, password = parts
        return f"http://{user}:{password}@{host}:{port}"
    return f"http://{rest}"


def _vault_cookies(url: str) -> dict:
    """Reuse Solver's clearance vault (~/.solver_clearance) if present."""
    p = Path.home() / ".solver_clearance" / f"{urlparse(url).netloc}.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text()).get("cookies", {})
    except Exception:
        return {}


class GhostSession:
    """One browsing identity. Use as a context manager.

    Engine ladder: CloakBrowser (best stealth) -> playwright Firefox
    (fallback — jis host pe CloakBrowser nahi). Dono pe same page API.
    """

    def __init__(
        self,
        profile: str = "default",
        proxy: str | None = None,
        headed: bool = False,
        humanize: bool = True,
        engine: str = "auto",  # auto | cloak | playwright
        **extra_launch_kwargs,
    ):
        self.profile_name = profile
        self.profile = load_profile(profile)
        self.proxy_url = _proxy_url(proxy)
        self.headed = headed
        self.humanize = humanize
        self.engine_pref = engine
        self.extra_launch_kwargs = extra_launch_kwargs
        self._pw = None
        self.browser = None
        self._ctx = None

    def __enter__(self):
        # Engine 0: GhostWire — APNA raw-CDP engine (no playwright dep,
        # no library CDP-pattern fingerprint). engine='wire' ya 'auto'
        # me pehla nahi — CloakBrowser zyada battle-tested hai isliye
        # auto me wire 2nd; explicit 'wire' pe primary.
        if self.engine_pref in ("auto", "cloak", "wire"):
            if self.engine_pref == "wire":
                try:
                    from ghostrise.wire import GhostWire
                    kwargs = {"headless": not self.headed}
                    if self.proxy_url:
                        kwargs["extra_args"] = [
                            f"--proxy-server={self.proxy_url.split('://')[-1]}"]
                    self.wire = GhostWire(**kwargs)
                    self.wire.launch()
                    self.browser = _WireCompat(self.wire)
                    return self
                except Exception as e:
                    raise RuntimeError(f"GhostWire launch fail: {e}")
        # Engine 1: CloakBrowser (engine-level stealth)
        if self.engine_pref in ("auto", "cloak"):
            try:
                from cloakbrowser import launch
                kwargs = {
                    "headless": not self.headed,
                    "humanize": self.humanize,
                    **self.extra_launch_kwargs,
                }
                if self.proxy_url:
                    kwargs["proxy"] = self.proxy_url
                try:
                    self.browser = launch(**kwargs)
                except TypeError:
                    kwargs.pop("humanize", None)
                    self.browser = launch(**kwargs)
                return self
            except ImportError:
                if self.engine_pref == "cloak":
                    raise RuntimeError("cloakbrowser requested but not installed")
        # Engine 2: playwright Firefox fallback (MOZ sandbox off for proot)
        import os
        os.environ.setdefault("MOZ_DISABLE_CONTENT_SANDBOX", "1")
        os.environ.setdefault("MOZ_DISABLE_GMP_SANDBOX", "1")
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        launch_kw = {"headless": not self.headed}
        if self.proxy_url:
            # user:pass@host:port -> playwright proxy dict
            import urllib.parse as _up
            pu = _up.urlparse(self.proxy_url)
            launch_kw["proxy"] = {
                "server": f"{pu.scheme or 'http'}://{pu.hostname}:{pu.port}",
                "username": pu.username or None,
                "password": pu.password or None,
            }
        self._ctx = self._pw.firefox.launch(**launch_kw).new_context(
            user_agent=self.profile.get("user_agent") or
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) "
            "Gecko/20100101 Firefox/131.0")
        self.browser = _PlaywrightCompat(self._ctx)
        return self

    def page(self, url: str | None = None):
        page = self.browser.new_page()
        # Replay vaulted Cloudflare clearance for this domain if we have it.
        if url:
            domain = urlparse(url).netloc
            vaulted = _vault_cookies(url).items()
            if vaulted:
                try:
                    self.browser.contexts[0].add_cookies(
                        [{"name": n, "value": v, "domain": "." + domain,
                          "path": "/"} for n, v in vaulted])
                except Exception:
                    pass
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        return page

    def human(self, page):
        """Human-shaped action wrappers for agents driving this page."""
        from ghostrise.behavior import HumanActions

        return HumanActions(page)

    def __exit__(self, *exc):
        if self._ctx:
            try:
                self._ctx.close()
            except Exception:
                pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
        if self.browser and not isinstance(self.browser, _PlaywrightCompat):
            try:
                self.browser.close()
            except Exception:
                pass
        return False


class _PlaywrightCompat:
    """CloakBrowser-style surface over a playwright browser context."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.contexts = [ctx]

    def new_page(self):
        return self.ctx.new_page()

    def close(self):
        try:
            self.ctx.close()
        except Exception:
            pass


class _WirePage:
    """GhostWire page — GhostSession ke page-interface jaisa surface
    (evaluate/title/text/mouse) taaki captcha_agent drop-in chale."""

    def __init__(self, wire):
        self.wire = wire

    def goto(self, url, **kw):
        self.wire.goto(url)
        return self

    def evaluate(self, expr, *a, **kw):
        return self.wire.evaluate(expr)

    @property
    def title(self):
        return self.wire.evaluate("document.title") or ""

    @property
    def url(self):
        return self.wire.evaluate("location.href") or ""

    def wait_for_timeout(self, ms):
        time.sleep(ms / 1000)

    @property
    def mouse(self):
        return self.wire.mouse


class _WireCompat:
    """GhostWire ko GhostSession browser-interface me adapt karta hai."""

    def __init__(self, wire):
        self.wire = wire
        self._page = None

    @property
    def contexts(self):
        class _C:
            def add_cookies(self, *a, **kw):
                pass
        return [_C()]

    def new_page(self):
        if self._page is None:
            self.wire._target()
            self._page = _WirePage(self.wire)
        return self._page

    def close(self):
        self.wire.close()


def open_url(url: str, profile: str = "default", proxy: str | None = None,
             headed: bool = False, screenshot: str | None = None):
    """One-shot helper: open, settle through challenges, report JSON."""
    with GhostSession(profile=profile, proxy=proxy, headed=headed) as ghost:
        page = ghost.page(url)
        deadline = time.time() + 45
        while time.time() < deadline:
            if "just a moment" not in page.title().lower():
                break
            page.wait_for_timeout(2000)
        title = page.title()
        cookies = {c["name"] for c in page.context.cookies()}
        result = {
            "url": url, "title": title,
            "cleared": "just a moment" not in title.lower(),
            "has_cf_clearance": "cf_clearance" in cookies,
            "ua": page.evaluate("navigator.userAgent"),
        }
        print(json.dumps(result, indent=2))
        if screenshot:
            page.screenshot(path=screenshot)
        return result
