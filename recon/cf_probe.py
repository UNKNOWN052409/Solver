"""Cloudflare challenge probe - tiered stealth testing through any exit IP.

Engines:
    requests    - raw HTTP client (tier 0 baseline)
    patchright  - runtime-patched chromium automation (tier 2)
Falls back to vanilla playwright if patchright isn't installed.

Proxy format: host:port:user:pass  |  user:pass@host:port  |  http://...
"""

import argparse
import json
import sys
import time

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Vendor-neutral challenge markers: title/body fragments that mean
# "an edge bot-wall is up", regardless of whose wall it is.
CHALLENGE_MARKERS = (
    "just a moment",              # Cloudflare managed challenge
    "vercel security checkpoint", # Vercel attack challenge mode
    "checking your browser",
    "attention required",
    "ddos protection by",
    "please wait while we verify",
)


def looks_challenged(status: int, text: str) -> bool:
    if status == 403 or status == 429:
        return True
    low = text.lower()
    return any(m in low for m in CHALLENGE_MARKERS)


def parse_proxy(p):
    """Normalize proxy string -> dict for playwright/patchright."""
    if not p:
        return None
    if p.startswith("http"):
        rest = p.split("://", 1)[1]
    else:
        rest = p
    creds = None
    if "@" in rest:
        creds, hostport = rest.rsplit("@", 1)
        user, _, password = creds.partition(":")
    else:
        parts = rest.split(":")
        if len(parts) == 4:
            host, port, user, password = parts
        else:
            hostport = rest
            user = password = None
    server = f"http://{hostport}" if ":" in hostport else f"http://{hostport}:80"
    out = {"server": server}
    if user:
        out["username"] = user
        out["password"] = password or ""
    return out


def probe_requests(url, proxy=None, timeout=30):
    import requests

    proxies = None
    if proxy:
        px = parse_proxy(proxy)["server"]
        proxies = {"http": px, "https": px}
    r = requests.get(
        url,
        headers={"User-Agent": UA},
        proxies=proxies,
        timeout=timeout,
        allow_redirects=True,
    )
    challenged = looks_challenged(r.status_code, r.text)
    return {
        "engine": "requests", "status": r.status_code,
        "outcome": "challenged" if challenged else "cleared",
        "title": "", "elapsed_s": round(r.elapsed.total_seconds(), 2),
        "cf_clearance": "cf_clearance" in r.cookies,
    }


def probe_browser(url, proxy=None, window=45, ua=None):
    try:
        from patchright.sync_api import sync_playwright
        engine_name = "patchright"
    except ImportError:
        from playwright.sync_api import sync_playwright
        engine_name = "playwright"

    pw_proxy = parse_proxy(proxy)
    with sync_playwright() as p:
        kwargs = {"headless": True}
        if pw_proxy:
            kwargs["proxy"] = pw_proxy
        browser = p.chromium.launch(**kwargs)
        # NOTE: if ua is None, headless builds leak "HeadlessChrome" into the
        # UA string - a trivial bot signal. Pass a real Chrome UA matching the
        # platform/geo of your exit for meaningful results.
        ctx_kwargs = {"locale": "en-US", "viewport": {"width": 1366, "height": 900}}
        if ua:
            ctx_kwargs["user_agent"] = ua
        elif engine_name == "playwright":
            ctx_kwargs["user_agent"] = UA  # mask the Headless token at least
        ctx = browser.new_context(**ctx_kwargs)
        page = ctx.new_page()
        t0 = time.time()
        resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)

        cleared = False
        while time.time() - t0 < window:
            # Text markers only: an in-page JS proof-of-work can clear
            # without a new navigation, so the original status goes stale.
            if not looks_challenged(0, page.content()):
                cleared = True
                break
            page.wait_for_timeout(1500)

        elapsed = time.time() - t0
        cookies = {c["name"]: c["value"] for c in ctx.cookies()}
        result = {
            "engine": engine_name, "status": resp.status if resp else 0,
            "outcome": "cleared" if cleared else "challenged",
            "title": page.title(), "elapsed_s": round(elapsed, 2),
            "cf_clearance": "cf_clearance" in cookies,
            "cookies": {k: v[:32] for k, v in cookies.items()},
        }
        browser.close()
        return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url")
    ap.add_argument("--engine", choices=["requests", "browser", "both"], default="both")
    ap.add_argument("--proxy", help="host:port:user:pass | user:pass@host:port | url")
    ap.add_argument("--ua", help="override user-agent (match your exit's real profile)")
    ap.add_argument("--window", type=int, default=45, help="challenge wait seconds")
    ap.add_argument("--json", dest="as_json", action="store_true")
    args = ap.parse_args()

    results = []
    engines = [args.engine] if args.engine != "both" else ["requests", "browser"]
    for e in engines:
        try:
            fn = probe_requests if e == "requests" else probe_browser
            if e == "requests":
                r = fn(args.url, args.proxy)
            else:
                r = fn(args.url, args.proxy, args.window, ua=args.ua)
            results.append(r)
            print(f"[{r['engine']:10s}] {r['outcome']:10s} http={r['status']} "
                  f"{r['elapsed_s']}s clearance={r['cf_clearance']}")
        except Exception as ex:
            results.append({"engine": e, "error": str(ex)[:200]})
            print(f"[{e:10s}] ERROR {str(ex)[:200]}")

    if args.as_json:
        print(json.dumps(results, indent=2))
    return 0 if any(r.get("outcome") == "cleared" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
