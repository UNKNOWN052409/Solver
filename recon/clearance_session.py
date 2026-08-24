"""Clearance session manager - mint once on YOUR device, replay everywhere.

Cloudflare's cf_clearance cookie stays valid for hours after the challenge
is passed once. So the practical workflow from your own residential IP:

    1. python3 clearance_session.py mint https://target.com
       -> opens a HEADED real browser window on this machine.
       If the challenge auto-clears, done. If it shows a checkbox,
       click it like a human. Cookie gets saved to the vault.

    2. Every later run replays the saved cookies + exact user-agent:

       python3 clearance_session.py test https://target.com
       -> prints whether the session still clears without challenge.

    3. When it expires, re-mint. That's the whole lifecycle.

Vault layout: ~/.solver_clearance/<domain>.json
    {url, user_agent, cookies {name: value}, minted_at, source_ip}
"""

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

VAULT_DIR = Path.home() / ".solver_clearance"

# Vendor-neutral challenge detection - single source of truth lives in
# cf_probe; mirror the markers here so both tools never disagree again.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cf_probe import looks_challenged  # noqa: E402
UA_FALLBACK = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def vault_path(url: str) -> Path:
    return VAULT_DIR / f"{urlparse(url).netloc}.json"


def save_entry(url: str, ua: str, cookies: dict):
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "url": url,
        "user_agent": ua,
        "cookies": cookies,
        "minted_at": time.time(),
        "source_ip": current_ip(),
    }
    vault_path(url).write_text(json.dumps(entry, indent=2))
    print(f"[+] vaulted clearance for {urlparse(url).netloc} -> {vault_path(url)}")
    return entry


def load_entry(url: str):
    p = vault_path(url)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def current_ip():
    try:
        return requests.get("https://api.ipify.org", timeout=10).text.strip()
    except Exception:
        return "?"


def cmd_mint(a):
    """Mint via the strongest engine available. CloakBrowser preferred -
    its headless clears challenges that hold against other engines."""
    try:
        from cloakbrowser import launch
        engine = "cloakbrowser"
    except ImportError:
        try:
            from patchright.sync_api import sync_playwright
            engine = "patchright"
        except ImportError:
            from playwright.sync_api import sync_playwright
            engine = "playwright"

    print(f"[*] minting via {engine} ({'headed' if a.headed else 'headless'})")

    def page_ua(p):
        try:
            return p.evaluate("navigator.userAgent")
        except Exception:
            return UA_FALLBACK

    def settled(ctx_cookies_getter, ctx_page):
        cookies = {c["name"]: c["value"] for c in ctx_cookies_getter()}
        has_token = "cf_clearance" in cookies or "_vcrcs" in cookies
        try:
            body_ok = not looks_challenged(0, ctx_page.content())
        except Exception:
            body_ok = False
        return (has_token and body_ok), cookies

    t0 = time.time()
    deadline = t0 + a.wait
    cookies, ua = None, None

    if engine == "cloakbrowser":
        browser = launch(headless=not a.headed, humanize=True)
        try:
            page = browser.new_page()
            page.goto(a.url, wait_until="domcontentloaded", timeout=60000)
            while time.time() < deadline:
                ok, cookies = settled(lambda: page.context.cookies(), page)
                if ok:
                    ua = page_ua(page)
                    break
                page.wait_for_timeout(2500)
        finally:
            browser.close()
    else:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not a.headed)
            ctx = browser.new_context(locale="en-US",
                                      viewport={"width": 1280, "height": 850})
            page = ctx.new_page()
            page.goto(a.url, wait_until="domcontentloaded", timeout=60000)
            while time.time() < deadline:
                ok, cookies = settled(lambda: ctx.cookies(), page)
                if ok:
                    ua = page_ua(page)
                    break
                page.wait_for_timeout(2500)
            browser.close()

    elapsed = time.time() - t0
    if not cookies or ua is None:
        print(f"[-] no clearance within {elapsed:.0f}s - challenge still up?")
        sys.exit(1)

    save_entry(a.url, ua, cookies)
    print(f"[+] CLEARED in {elapsed:.0f}s | cookies: {sorted(cookies)}")


def cmd_test(a):
    entry = load_entry(a.url)
    if not entry:
        print(f"[!] no vault entry for {a.url} - run 'mint' first")
        sys.exit(2)

    age_min = (time.time() - entry["minted_at"]) / 60
    now_ip = current_ip()
    print(f"[*] vault age: {age_min:.0f} min | minted from IP {entry['source_ip']} | now on {now_ip}")
    if now_ip != entry["source_ip"]:
        print("[!] WARNING: your IP changed since minting - clearance will NOT apply")

    r = requests.get(
        a.url,
        headers={"User-Agent": entry["user_agent"], **entry.get("headers", {})},
        cookies=entry["cookies"],
        timeout=30,
    )
    challenged = looks_challenged(r.status_code, r.text)
    print(f"[*] replay result: HTTP {r.status_code} | {'CHALLENGED' if challenged else 'CLEARED'}")
    if not challenged:
        print(f"[*] body starts: {r.text[:150]!r}")
    else:
        print("[*] clearance expired or invalid -> re-mint")


def cmd_status(a):
    e = load_entry(a.url)
    if not e:
        print("[!] nothing vaulted")
        sys.exit(2)
    age = (time.time() - e["minted_at"]) / 60
    print(f"target     : {e['url']}")
    print(f"age        : {age:.0f} min")
    print(f"user_agent : {e['user_agent'][:70]}...")
    print(f"ip         : {e['source_ip']}")
    print(f"cookies    : {', '.join(e['cookies'])}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("mint", help="pass the challenge once, save clearance to vault")
    m.add_argument("url")
    m.add_argument("--headed", action="store_true",
                   help="visible window (for checkbox challenges)")
    m.add_argument("--wait", type=int, default=180, help="seconds to wait for clearance")
    m.set_defaults(fn=cmd_mint)

    t = sub.add_parser("test", help="replay vaulted clearance against target")
    t.add_argument("url")
    t.set_defaults(fn=cmd_test)

    s = sub.add_parser("status", help="show vault entry info")
    s.add_argument("url")
    s.set_defaults(fn=cmd_status)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
