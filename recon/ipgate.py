"""IPGate - 24/7 self-healing gateway for instantproxies.com.

Maintains a fresh Vercel clearance (_vcrcs, 1h TTL) automatically and
serves a local HTTP API so your tasks can GET/POST without ever seeing
the checkpoint:

    GET  http://127.0.0.1:8899/status
    GET  http://127.0.0.1:8899/get?path=/some/page
    POST http://127.0.0.1:8899/post?path=/api/whatever
         body: raw payload, Content-Type forwarded as-is

Self-healing loop:
    - keeper thread probes the site every KEEPALIVE_SECS
    - if challenged (429/checkpoint) -> re-mint via CloakBrowser headless
    - mint also forced when vault older than REFRESH_SECS (< TTL)

Run:
    python3 recon/ipgate.py [--port 8899] [--mint-now]
"""

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cf_probe import looks_challenged  # noqa: E402

BASE = "https://instantproxies.com"
VAULT = Path.home() / ".solver_clearance" / "instantproxies.com.json"
LOG_FILE = Path("/tmp/opencode/ipgate.log")

REFRESH_SECS = 45 * 60      # proactively refresh before the 60-min TTL
KEEPALIVE_SECS = 8 * 60     # probe cadence
MINT_WAIT = 90              # seconds allowed for a challenge solve

state_lock = threading.Lock()
state = {"session": None, "ua": None, "minted_at": 0.0, "ip": "?", "last_probe": 0.0}


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def current_ip():
    try:
        return requests.get("https://api.ipify.org", timeout=10).text.strip()
    except Exception:
        return "?"


def load_vault():
    if VAULT.exists():
        e = json.loads(VAULT.read_text())
        state["session"] = e.get("cookies", {})
        state["ua"] = e.get("user_agent")
        state["minted_at"] = e.get("minted_at", 0)
        state["ip"] = e.get("source_ip", "?")


def save_vault(cookies, ua):
    VAULT.parent.mkdir(parents=True, exist_ok=True)
    VAULT.write_text(json.dumps({
        "url": BASE, "user_agent": ua, "cookies": cookies,
        "minted_at": time.time(), "source_ip": current_ip(),
    }, indent=2))


def mint():
    """Fresh clearance via CloakBrowser (headless). Returns bool."""
    try:
        from cloakbrowser import launch
    except ImportError:
        log("[!] cloakbrowser missing - cannot mint")
        return False

    t0 = time.time()
    log("[*] minting fresh clearance...")
    with launch(headless=True, humanize=True) as browser:
        page = browser.new_page()
        page.goto(BASE, wait_until="domcontentloaded", timeout=60000)
        deadline = t0 + MINT_WAIT
        while time.time() < deadline:
            cookies = {c["name"]: c["value"] for c in page.context.cookies()}
            if "_vcrcs" in cookies:
                try:
                    ok = not looks_challenged(0, page.content())
                except Exception:
                    ok = True
                if ok:
                    ua = page.evaluate("navigator.userAgent")
                    with state_lock:
                        state["session"] = cookies
                        state["ua"] = ua
                        state["minted_at"] = time.time()
                        state["ip"] = current_ip()
                    save_vault(cookies, ua)
                    log(f"[+] minted in {time.time()-t0:.0f}s | "
                        f"cookies={sorted(cookies)} | ip={state['ip']}")
                    return True
            page.wait_for_timeout(2500)
    log("[-] mint failed within window")
    return False


def probe():
    """True if current session clears the edge."""
    s = state.get("session")
    if not s:
        return False
    try:
        r = requests.get(BASE, headers={"User-Agent": state["ua"]},
                         cookies=s, timeout=20)
        ok = not looks_challenged(r.status_code, r.text)
        return ok
    except Exception as e:
        log(f"[!] probe error: {e}")
        return False


def ensure_fresh(force=False):
    with state_lock:
        age = time.time() - state["minted_at"]
    if force or not state["session"] or age > REFRESH_SECS:
        log(f"[*] refresh needed (age {age/60:.0f} min)")
        if not mint():
            # one retry after short cool-down
            time.sleep(5)
            return mint()
    elif not probe():
        log("[*] probe failed despite fresh vault -> forcing mint")
        return mint()
    return True


def keeper_loop():
    while True:
        try:
            ensure_fresh()
        except Exception as e:
            log(f"[!] keeper error: {e}")
        time.sleep(KEEPALIVE_SECS)


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/status":
            with state_lock:
                return self._json({
                    "ok": True,
                    "vault_age_min": round((time.time()-state["minted_at"])/60, 1),
                    "ip": state["ip"],
                    "has_session": bool(state["session"]),
                    "ttl_left_min": max(0, round(60 - (time.time()-state["minted_at"])/60, 1)),
                })
        if u.path == "/get":
            path = q.get("path", ["/"])[0]
            with state_lock:
                cookies, ua = dict(state["session"]), state["ua"]
            r = requests.get(BASE + path, headers={"User-Agent": ua},
                             cookies=cookies, timeout=30)
            challenged = looks_challenged(r.status_code, r.text)
            if challenged and mint():
                with state_lock:
                    cookies, ua = dict(state["session"]), state["ua"]
                r = requests.get(BASE + path, headers={"User-Agent": ua},
                                 cookies=cookies, timeout=30)
                challenged = looks_challenged(r.status_code, r.text)
            return self._json({"path": path, "status": r.status_code,
                               "challenged": challenged, "bytes": len(r.content),
                               "body_head": r.text[:200]})
        return self._json({"error": "unknown route"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/post":
            return self._json({"error": "unknown route"}, 404)
        q = parse_qs(u.query)
        path = q.get("path", ["/"])[0]
        length = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type", "application/json")
        with state_lock:
            cookies, ua = dict(state["session"]), state["ua"]
        r = requests.post(BASE + path, data=payload,
                          headers={"User-Agent": ua, "Content-Type": ctype},
                          cookies=cookies, timeout=30)
        challenged = looks_challenged(r.status_code, r.text)
        if challenged and mint():
            with state_lock:
                cookies, ua = dict(state["session"]), state["ua"]
            r = requests.post(BASE + path, data=payload,
                              headers={"User-Agent": ua, "Content-Type": ctype},
                              cookies=cookies, timeout=30)
            challenged = looks_challenged(r.status_code, r.text)
        return self._json({"path": path, "status": r.status_code,
                           "challenged": challenged,
                           "response_head": r.text[:300]})

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--mint-now", action="store_true",
                    help="force immediate re-mint on start")
    args = ap.parse_args()

    log("[*] IPGate starting...")
    load_vault()
    if args.mint_now:
        mint()
    else:
        ensure_fresh()

    threading.Thread(target=keeper_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    log(f"[+] gateway ready -> http://127.0.0.1:{args.port}  (/status /get /post)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
