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
    """Normalize any common proxy notation to a URL string for cloakbrowser."""
    if not proxy:
        return None
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
    """One browsing identity. Use as a context manager."""

    def __init__(
        self,
        profile: str = "default",
        proxy: str | None = None,
        headed: bool = False,
        humanize: bool = True,
        **extra_launch_kwargs,
    ):
        self.profile_name = profile
        self.profile = load_profile(profile)
        self.proxy_url = _proxy_url(proxy)
        self.headed = headed
        self.humanize = humanize
        self.extra_launch_kwargs = extra_launch_kwargs
        self._pw = None
        self.browser = None

    def __enter__(self):
        try:
            from cloakbrowser import launch
        except ImportError as e:
            raise RuntimeError(
                "GhostRise needs the CloakBrowser engine on this host: "
                "pip install cloakbrowser"
            ) from e

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
            # Version drift: drop newer kwargs an older wrapper rejects.
            kwargs.pop("humanize", None)
            self.browser = launch(**kwargs)
        return self

    def page(self, url: str | None = None):
        page = self.browser.new_page()
        # Replay vaulted Cloudflare clearance for this domain if we have it.
        if url:
            domain = urlparse(url).netloc
            vaulted = _vault_cookies(url).items()
            if vaulted:
                self.browser.contexts[0].add_cookies(
                    [{"name": n, "value": v, "domain": "." + domain, "path": "/"}
                     for n, v in vaulted]
                )
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        return page

    def human(self, page):
        """Human-shaped action wrappers for agents driving this page."""
        from ghostrise.behavior import HumanActions

        return HumanActions(page)

    def __exit__(self, *exc):
        if self.browser:
            try:
                self.browser.close()
            except Exception:
                pass
        return False


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
