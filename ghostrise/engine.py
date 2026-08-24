"""GhostRise - anti-detect browsing engine.

v0.1 rides the Camoufox engine (Firefox fork with C++-level fingerprint
spoofing) under a unified control layer:

    - headed AND headless share the SAME rendering path -> identical
      fingerprints in both modes (no HeadlessChrome-class leaks)
    - persistent per-profile identities (stable spoof across sessions)
    - native proxy support incl. geo-matched locales
    - automatic replay of any vaulted cf_clearance for the target

Roadmap to a full self-built engine lives in README_GHOSTRISE.md.
"""

import json
import time
from pathlib import Path
from urllib.parse import urlparse

from ghostrise.profiles import load_profile

DEFAULT_ARGS = {"block_webrtc": True}


def _proxy_dict(proxy: str | None):
    """user:pass@host:port | host:port:user:pass | url -> camoufox/playwright dict"""
    if not proxy:
        return None
    rest = proxy.split("://", 1)[-1]
    if "@" in rest:
        creds, hostport = rest.rsplit("@", 1)
        user, _, password = creds.partition(":")
    else:
        parts = rest.split(":")
        if len(parts) == 4:
            host, port, user, password = parts
            hostport = f"{host}:{port}"
        else:
            hostport = rest
            user = password = None
    server = hostport if hostport.startswith("http") else f"http://{hostport}"
    out = {"server": server}
    if user:
        out["username"], out["password"] = user, password or ""
    return out


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
        geo_match: bool = True,
        humanize: bool = True,
    ):
        self.profile_name = profile
        self.profile = load_profile(profile)
        self.proxy = _proxy_dict(proxy)
        self.headed = headed
        self.geo_match = geo_match
        self.humanize = humanize
        self._cm = None
        self.browser = None

    def __enter__(self):
        try:
            from camoufox.sync_api import Camoufox
        except ImportError as e:
            raise RuntimeError(
                "GhostRise engine needs camoufox on the HOST machine: "
                "pip install 'camoufox[geoip]' && python3 -m camoufox fetch"
            ) from e

        prof = self.profile
        kwargs = {
            "headless": not self.headed,
            "geoip": self.geo_match,
            "humanize": self.humanize,
            "i18n": prof.get("locale", "en-US"),
            # Stable identity: same profile -> same spoof inputs every time.
            "fp_config": {
                "os": prof.get("os", "windows"),
                "locale": prof.get("locale", "en-US"),
                "screen": prof.get("screen", [1366, 900]),
                "hardware_concurrency": prof.get("cores", 8),
                **prof.get("fp_overrides", {}),
            },
        }
        if self.proxy:
            kwargs["proxy"] = self.proxy

        try:
            self._cm = Camoufox(**{**kwargs, **DEFAULT_ARGS})
            self.browser = self._cm.__enter__()
        except TypeError:
            # Older camoufox builds: drop kwargs they don't know.
            for k in ("humanize", "fp_config"):
                kwargs.pop(k, None)
            self._cm = Camoufox(**{**kwargs, **DEFAULT_ARGS})
            self.browser = self._cm.__enter__()
        return self

    def page(self, url: str | None = None):
        ctx = self.browser.new_context()
        # Replay vaulted Cloudflare clearance for this domain if we have it.
        if url:
            domain = urlparse(url).netloc
            vaulted = _vault_cookies(url).items()
            if vaulted:
                ctx.add_cookies(
                    [{"name": n, "value": v, "domain": "." + domain, "path": "/"}
                     for n, v in vaulted]
                )
        page = ctx.new_page()
        if url:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        return page

    def __exit__(self, *exc):
        if self._cm:
            return self._cm.__exit__(*exc)


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
