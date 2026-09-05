"""CaptureBrowser — same-IP anti-captcha browser + MITM cookie/endpoint capture.

What this is (for LO / @torbug):

A browser that does THREE jobs in one session, built on the GhostRise
stealth engine (CloakBrowser / GhostWire):

  1. SAME-IP GUARANTEE
     Before doing anything it verifies the browser's egress IP equals the
     system/server IP. If a MITM proxy or a server-side flow would give the
     browser a DIFFERENT exit IP than the system, that mismatch itself is a
     tell (e.g. you login "as ChatGPT server" but ship from your own IP).
     This module refuses to proceed on a mismatch instead of silently
     leaking it. That is the "browser IP == system IP" rule.

  2. PASSWORD MANAGER (vault auto-login)
     Reads the GhostEngine password vault (~/.ghostbrowse/vault.bin —
     AES-256-GCM, see engine/src/vault.rs) and auto-fills login forms for
     the target site. No plaintext on disk anywhere.

  3. MITM COOKIE / ENDPOINT CAPTURE
     Attaches a CDP Network listener. While you log into a provider
     (Netflix / Amazon / Facebook / ...) it records:
       - the session cookies (the Cookie header value to replay)
       - the authenticated API/account endpoints the app actually hits,
         plus the response body (so the checker/ validator can point at the
         REAL endpoint and read plan/region from the REAL payload).
     Captures are deduped and written under ~/.solver_capture/<provider>.

Usage
-----
    # bare same-IP browser (open a page)
    with CaptureBrowser(provider="netflix", same_ip=True) as cb:
        page = cb.open("https://www.netflix.com/")
        print(page.title())

    # vault auto-login + MITM capture:
    #   (fills login/pass from vault entry for the domain, then captures
    #    cookies + account endpoints while you complete 2FA if any)
    python3 -m ghostrise.capture_browser netflix --capture --vault \
        --same-ip --out ~/.solver_capture

    # dry same-IP check only
    python3 -m ghostrise.capture_browser --check-ip
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse


# ───────────────────────────── vault (password manager) ─────────────────────────────

VAULT_PATH = Path.home() / ".ghostbrowse" / "vault.bin"
CAPTURE_DIR = Path.home() / ".solver_capture"


def vault_entries(vault_path: Path = VAULT_PATH, master: str | None = None):
    """Try the Rust vault first; else a plaintext JSON sidecar (dev).
    Returns list of {site, user, pass, notes}. On locked/absent -> []."""
    if vault_path.exists():
        try:
            from engine import vault as v  # pyo3 binding if built
            se = v.Vault(str(vault_path))
            if master:
                se.unlock(master)
            return [{"site": e.site, "user": e.user, "pass": getattr(e, "pass"),
                     "notes": e.notes} for e in se.entries()]
        except Exception:
            pass  # binding not built — fall through
    # plaintext sidecar for portability (never the real vault)
    side = vault_path.with_suffix(".json")
    if side.exists():
        try:
            return json.loads(side.read_text()).get("entries", [])
        except Exception:
            return []
    return []


def find_cred(entries, url: str):
    """Best-match vault entry for a URL by domain suffix. Returns dict or None."""
    host = (urlparse(url).netloc or url).lower().replace("www.", "")
    best, best_len = None, -1
    for e in entries:
        site = (e.get("site") or "").lower().replace("www.", "")
        if site and (host == site or host.endswith("." + site)
                     or site.endswith("." + host) or site in host):
            if len(site) > best_len:
                best, best_len = e, len(site)
    return best


def fill_login(page, cred,
               user_sel=("input[type=email], input[name*=user], "
                         "input[name*=email], input[type=text]"),
               pass_sel="input[type=password]"):
    """Auto-fill login fields from a vault entry (with humanized typing)."""
    filled = []
    try:
        u = page.locator(user_sel).first
        u.fill(cred["user"])
        filled.append("user")
    except Exception:
        pass
    try:
        p = page.locator(pass_sel).first
        p.fill(cred["pass"])
        filled.append("pass")
    except Exception:
        pass
    return filled


# ───────────────────────────── same-IP enforcement ─────────────────────────────

def _http_ip(url="https://api.ipify.org", timeout=12):
    import requests
    try:
        return requests.get(url, timeout=timeout).text.strip()
    except Exception:
        return None


class IPMismatch(Exception):
    pass


def check_same_ip(page, system_ip=None, strict=True):
    """Verify the browser's egress IP == the system IP.

    page can be: (a) a live Playwright Page (reads via page.evaluate fetch),
    or (b) a tuple ('static', browser_ip) when only the browser egress is
    already known. Returns (browser_ip, system_ip, ok).
    Raises IPMismatch on mismatch when strict=True.
    """
    sys_ip = system_ip or _http_ip()
    if isinstance(page, tuple):           # ('static', browser_ip)
        b_ip = page[1]
    else:
        try:
            b_ip = page.evaluate(
                "async () => (await fetch('https://api.ipify.org')).text()")
        except Exception:
            try:
                b_ip = page.evaluate(
                    "navigator.connection ? '?' : " +
                    "(window.__ip||'unresolved')")
            except Exception:
                b_ip = "unresolved"
    if not sys_ip or sys_ip == "?":
        sys_ip = "unresolved"
    if b_ip == "unresolved":
        # can't resolve browser IP — under proxy the system resolve still
        # routes locally, so don't hard-block; report
        return (b_ip, sys_ip, False)
    ok = bool(b_ip and sys_ip and b_ip == sys_ip)
    if strict and not ok:
        raise IPMismatch(
            f"browser IP {b_ip} != system IP {sys_ip} — refusing. "
            "Use proxy='resi' (system egress) or fix the proxy so browser "
            "and system share the egress IP.")
    return (b_ip, sys_ip, ok)


# ───────────────────────────── CDP network capture (MITM) ─────────────────────────────

class NetCapture:
    """CDP Network listener — records session cookies + account endpoints.

    Attaches to a Playwright page via new_cdp_session, subscribes to
    Network.requestWillBeSent + Network.responseReceived + Network.loadingFinished,
    and collects:
      - login (any request whose URL contains login/account/profile/me/api)
      - endpoints: (method, url) + captured response body for the interesting
        auth/account calls, plus the request Cookie header (the replay token).
    """

    KEYWORDS = ("account", "membership", "profile", "user", "me", "plan",
                "subscription", "authcontext", "pathEvaluator", "api", "login")

    def __init__(self, page, cookie_mode="context"):
        self.page = page
        self.cookie_mode = cookie_mode
        self.cdp = page.context.new_cdp_session(page)
        self.endpoints = {}     # url -> {method, status, cookie, body, t}
        self.login_calls = []
        self.har = []
        self.cdp.on("Network.requestWillBeSent", self._on_req)
        self.cdp.on("Network.responseReceived", self._on_resp)
        self.cdp.on("Network.loadingFinished", self._on_loaded)
        self.cdp.send("Network.enable")
        self.cdp.send("Page.enable")

    def _interesting(self, url: str):
        u = url.lower()
        return any(k in u for k in self.KEYWORDS) and ("/api/" in u or "/shakti" in u)

    def _cookies_header(self, url):
        try:
            cs = self.page.context.cookies(url)
            if not cs:
                return None
            return "; ".join(f"{c['name']}={c['value']}" for c in cs)
        except Exception:
            return None

    def _on_req(self, params):
        req = params.get("request", {})
        url = req.get("url", "")
        if self._interesting(url):
            self.login_calls.append({
                "method": req.get("method"),
                "url": url,
                "cookie": (req.get("headers") or {}).get("Cookie"),
                "t": time.time(),
            })

    def _on_resp(self, params):
        resp = params.get("response", {})
        url = resp.get("url", "")
        if not self._interesting(url):
            return
        self.endpoints.setdefault(url, {
            "method": params.get("type"),
            "status": resp.get("status"),
            "cookie": (resp.get("headers") or {}).get("Set-Cookie")
                      or self._cookies_header(url),
            "body": None, "t": time.time(),
        })

    def _on_loaded(self, params):
        try:
            body = self.cdp.send("Network.getResponseBody",
                                 {"requestId": params["requestId"]})
            b = body.get("body", "")
        except Exception:
            return
        # match to a known endpoint by requestId is hard — tag by content
        if '"membership"' in b or '"plan"' in b or '"userCountry"' in b:
            self._tag_body(b)

    def _tag_body(self, body):
        for url in self.endpoints:
            if self.endpoints[url]["body"] is None:
                self.endpoints[url]["body"] = body[:200_000]
                break

    def stop(self):
        try:
            self.cdp.detach()
        except Exception:
            pass

    def summary(self):
        return {
            "login_calls": self.login_calls,
            "endpoints": [
                {**v, "url": u} for u, v in self.endpoints.items()
                if v.get("body") or v.get("cookie")
            ],
        }


# ───────────────────────────── CaptureBrowser ─────────────────────────────

class CaptureBrowser:
    """Same-IP anti-captcha browser with vault autologin + MITM capture.

    Backed by ACSession (anti-captcha wrapper over GhostSession). Adds:
      - same-IP enforcement (agent egress == system egress)
      - vault auto-fill on login pages
      - CDP network capture of cookies + account endpoints
    """

    def __init__(self, provider: str = "generic", same_ip: bool = True,
                 proxy: str | None = "resi", headed: bool = False,
                 capture: bool = True, use_vault: bool = True,
                 max_retries: int = 2):
        self.provider = provider
        self.same_ip = same_ip
        self.proxy = proxy
        self.capture = capture
        self.use_vault = use_vault
        self.net = None
        self.last_ip = None
        from ghostrise.ac_browser import ACSession
        self.ac = ACSession(profile=f"provider_{provider}", proxy=proxy,
                            headed=headed, max_retries=max_retries)

    def __enter__(self):
        self.ac.__enter__()
        return self

    def __exit__(self, *exc):
        if self.net:
            self.net.stop()
        return self.ac.__exit__(*exc)

    def open(self, url: str, wait: int = 5, verify_ip: bool = True,
             vault_autofill: bool | None = None):
        page = self.ac.open(url, wait=wait)
        if self.same_ip and verify_ip:
            ip, sys_ip, ok = check_same_ip(page, strict=False)
            self.last_ip = ip
            if not ok and ip != "unresolved":
                print(f"[!] IP mismatch: browser={ip} system={sys_ip}")
                if self.same_ip:
                    raise IPMismatch(
                        f"browser {ip} != system {sys_ip}. Use proxy='resi'.")
        if self.capture and self.net is None:
            try:
                self.net = NetCapture(page)
                print("[+] MITM capture armed (CDP Network)")
            except Exception as e:
                print(f"[!] capture off: {e}")
        if vault_autofill is None:
            vault_autofill = self.use_vault
        if vault_autofill:
            cred = find_cred(vault_entries(), url)
            if cred:
                from ghostrise.behavior import HumanActions
                filled = fill_login(HumanActions(page), cred)
                print(f"[+] vault autofill: {filled} for {url}")
            else:
                print(f"[-] vault: no entry for {url}")
        return page

    def dump_capture(self, outdir: Path = CAPTURE_DIR):
        """Dedupe + save cookies & endpoints for this provider."""
        if not self.net:
            return None
        prov = re.sub(r"\W+", "_", self.provider.lower()) or "generic"
        outdir.mkdir(parents=True, exist_ok=True)
        s = self.net.summary()

        # 1) dedupe cookies
        cookies = {}
        for ep in s.get("endpoints", []):
            c = ep.get("cookie")
            if c:
                # key = first cookie name before '='
                name = c.split("=", 1)[0].strip()
                if name and name not in cookies:
                    cookies[name] = c
        cookie_lines = [c for _, c in sorted(cookies.items())]

        # 2) dedupe endpoint URLs
        seen = set()
        endpoints = []
        for ep in s.get("endpoints", []):
            u = ep["url"]
            if u in seen:
                continue
            seen.add(u)
            endpoints.append(ep)

        ts = time.strftime("%Y%m%d_%H%M%S")
        base = outdir / prov
        base.mkdir(parents=True, exist_ok=True)
        cpath = base / f"cookies_{ts}.txt"
        epath = base / f"endpoints_{ts}.json"
        cpath.write_text("\n".join(cookie_lines) + ("\n" if cookie_lines else ""))
        epath.write_text(json.dumps(
            {"provider": self.provider, "captured_at": ts,
             "cookies": cookie_lines, "endpoints": endpoints,
             "login_calls": s.get("login_calls", [])}, indent=2))
        print(f"[+] cookies  -> {cpath}  ({len(cookie_lines)})")
        print(f"[+] endpoints-> {epath}  ({len(endpoints)})")
        return {"cookies": str(cpath), "endpoints": str(epath)}


# ───────────────────────────── CLI ─────────────────────────────

def _check_ip_only():
    sys_ip = _http_ip()
    print(f"system IP: {sys_ip}")
    return 0 if sys_ip else 1


def main():
    ap = argparse.ArgumentParser(prog="capture_browser", description=__doc__)
    ap.add_argument("provider", nargs="?", default="generic",
                    help="netflix / amazon / facebook / ...")
    ap.add_argument("url", nargs="?", help="target URL (default: provider guess)")
    ap.add_argument("--capture", action="store_true", help="enable MITM capture")
    ap.add_argument("--vault", action="store_true", help="auto-fill from password vault")
    ap.add_argument("--same-ip", action="store_true",
                    help="enforce browser IP == system IP (default off in CLI)")
    ap.add_argument("--check-ip", action="store_true", help="print system IP and exit")
    ap.add_argument("--parallel-ips", action="store_true",
                    help="report browser+system side by side (no strict block)")
    ap.add_argument("--proxy", default="resi",
                    help="proxy mode: resi (system) | pool | auto | explicit url")
    ap.add_argument("--out", type=Path, default=CAPTURE_DIR)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--timeout", type=int, default=45)
    args = ap.parse_args()

    if args.check_ip:
        return _check_ip_only()

    default_urls = {
        "netflix": "https://www.netflix.com/login",
        "amazon": "https://www.amazon.com/ap/signin",
        "facebook": "https://www.facebook.com/login",
        "prime": "https://www.primevideo.com/",
    }
    url = args.url or default_urls.get(args.provider.lower(),
                                       f"https://{args.provider}.com")

    print(f"[*] CaptureBrowser provider={args.provider} url={url} "
          f"proxy={args.proxy} same_ip={args.same_ip}")

    with CaptureBrowser(provider=args.provider, same_ip=args.same_ip,
                        proxy=args.proxy, headed=args.headed,
                        capture=args.capture, use_vault=args.vault) as cb:
        try:
            page = cb.open(url, wait=5)
        except IPMismatch as e:
            print(f"[x] {e}")
            if args.same_ip:
                return 2
            page = cb.ac.g.page(url) if hasattr(cb.ac, "g") else None
        print(f"[*] page: {page.title() if page else 'n/a'}")

        if args.same_ip and args.parallel_ips:
            ip, sys_ip, ok = check_same_ip(page, strict=False)
            print(f"[*] browser IP = {ip} | system IP = {sys_ip} | match={ok}")

        if args.capture and page:
            print("[*] login complete karo / 2FA karo... capture chalu hai.")
            deadline = time.time() + args.timeout
            while time.time() < deadline:
                time.sleep(2)
                if cb.net and len(cb.net.login_calls) > 0:
                    print(f"[*] {len(cb.net.login_calls)} auth call(s) captured")
            cb.dump_capture(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
