"""GhostPipe - universal anti-bot gateway.

One endpoint for ANY url. Classifies the wall, picks a strategy,
maintains per-domain clearance vaults, and logs every outcome as
telemetry so the system's success rates become training data.

    GET  http://127.0.0.1:8900/fetch?url=<any-url>
    POST http://127.0.0.1:8900/fetch   {"url":..., "method":"POST", "body":{...}, "headers":{...}}
    GET  http://127.0.0.1:8900/stats   <- live success rates per domain & wall class

Strategy ladder per wall class:
    none            -> plain requests
    cf_managed      -> domain vault (_cf_clearance/_vcrcs style) -> browser mint -> retry
    vercel_checkpoint-> same as cf_managed (cookie _vcrcs)
    widget_captcha  -> browser-tier render (invisible challenges auto-pass)
    hard_block      -> report honestly, no fake success

Every response appends to ~/.ghostpipe/telemetry.jsonl - the flywheel:
jitna solve karega, utna data, utna upgrade.
"""

import json
import sys
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs  # noqa: F401 — parse_qs used below

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cf_probe import looks_challenged  # noqa: E402

TELEMETRY_DIR = Path.home() / ".ghostpipe"
TELEMETRY_FILE = TELEMETRY_DIR / "telemetry.jsonl"
UA_FALLBACK = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

lock = threading.Lock()


# ---- wall classification ---------------------------------------------------

WALL_MARKERS = {
    "cf_managed": ("just a moment", "challenge-platform", "cf-challenge"),
    "vercel_checkpoint": ("vercel security checkpoint", "_vcrcs",),
    "generic_pow": ("checking your browser", "ddos protection by",
                    "please wait while we verify"),
}


def classify(status: int, text: str) -> str:
    low = text.lower()
    if status in (403, 429):
        if status == 429 and "vercel security checkpoint" in low:
            return "vercel_checkpoint"
        for cls, markers in WALL_MARKERS.items():
            if any(m in low for m in markers):
                return cls
        return "hard_block"
    # 200 OK: interstitial shells put their title early in the document;
    # scanning deep into big SPA bundles causes false positives.
    if len(text) < 8000:
        for cls, markers in WALL_MARKERS.items():
            if any(m in low[:2000] for m in markers):
                return cls
    return "none"


def vault_path(url: str) -> Path:
    return Path.home() / ".solver_clearance" / f"{urlparse(url).netloc}.json"


def load_vault(url):
    p = vault_path(url)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def save_vault(url, ua, cookies):
    vault_path(url).write_text(json.dumps({
        "url": url, "user_agent": ua, "cookies": cookies,
        "minted_at": time.time(), "source_ip": "?",
    }, indent=2))


# ---- strategies --------------------------------------------------------------

def strategy_direct(url, method="GET", body=None, headers=None):
    h = {"User-Agent": UA_FALLBACK, **(headers or {})}
    r = requests.request(method, url, headers=h, timeout=30,
                         data=json.dumps(body).encode() if body else None)
    return r


def strategy_vault(url, method="GET", body=None, headers=None):
    """Replay vaulted cookies + exact UA. Works while TTL alive."""
    entry = load_vault(url)
    if not entry:
        return None
    h = {"User-Agent": entry["user_agent"], **(headers or {})}
    r = requests.request(method, url, headers=h,
                         cookies=entry.get("cookies", {}), timeout=30,
                         data=json.dumps(body).encode() if body else None)
    return r


def strategy_browser_mint(url, method="GET", body=None, headers=None):
    """Open CloakBrowser headless, cross whatever wall, save clearance."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from cloakbrowser import launch
    except ImportError:
        return None

    with launch(headless=True, humanize=True) as browser:
        page = browser.new_page()
        t0 = time.time()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        while time.time() - t0 < 75:
            try:
                content = page.content()
                if not looks_challenged(200, content):
                    break
            except Exception:
                pass
            page.wait_for_timeout(2500)

        ua = page.evaluate("navigator.userAgent")
        cookies = {c["name"]: c["value"] for c in page.context.cookies()}
        final_ok = not looks_challenged(200, page.content())
        if final_ok:
            save_vault(url, ua, cookies)
        else:
            return None  # never poison the vault with a failed session

        # same-origin fetch passthrough for POST bodies etc.
        if method != "GET":
            res = page.evaluate("""async ([u, m, b]) => {
                const r = await fetch(u, {method: m,
                    headers: {'Content-Type': 'application/json'},
                    body: b ? JSON.stringify(b) : undefined});
                return {s: r.status, b: (await r.text()).slice(0, 500)};
            }""", [url, method, body])
            class FakeResp:
                status_code = res["s"]
                text = res["b"]
                content = res["b"].encode()
            return FakeResp()

        # GET: reload through cleared session for final content
        page.reload(wait_until="domcontentloaded")
        class BrowserResp:
            status_code = 200
            text = page.content()
            content = text.encode()
        return BrowserResp()


def route(url, method="GET", body=None, headers=None):
    """Try strategies in order until one clears. Returns (resp_meta, attempts)."""
    attempts = []
    resp = strategy_direct(url, method, body, headers)
    cls = classify(resp.status_code, resp.text)
    attempts.append({"strategy": "direct", "status": resp.status_code, "class": cls})
    if cls == "none":
        return {"status": resp.status_code, "class": cls, "via": "direct",
                "bytes": len(resp.content), "head": resp.text[:300]}, attempts

    resp = strategy_vault(url, method, body, headers)
    if resp is not None:
        cls2 = classify(resp.status_code, resp.text)
        attempts.append({"strategy": "vault_replay", "status": resp.status_code, "class": cls2})
        if cls2 == "none":
            return {"status": resp.status_code, "class": cls2, "via": "vault_replay",
                    "bytes": len(resp.content), "head": resp.text[:300]}, attempts

    resp = strategy_browser_mint(url, method, body, headers)
    if resp is not None:
        cls3 = classify(getattr(resp, "status_code", 200), getattr(resp, "text", ""))
        attempts.append({"strategy": "browser_mint", "status": getattr(resp, "status_code", "?"),
                         "class": cls3})
        if cls3 == "none":
            return {"status": getattr(resp, "status_code", 200), "class": cls3,
                    "via": "browser_mint", "bytes": len(resp.content),
                    "head": resp.text[:300]}, attempts

    last_cls = attempts[-1]["class"] if attempts else "unknown"
    return {"status": attempts[-1]["status"] if attempts else 0,
            "class": last_cls, "via": "exhausted",
            "bytes": len(getattr(resp, "content", b"")),
            "head": getattr(resp, "text", "")[:300]}, attempts


def record_telemetry(domain, wall_class, via, ok, elapsed):
    TELEMETRY_DIR.mkdir(exist_ok=True)
    rec = {"ts": time.time(), "domain": domain, "wall": wall_class,
           "via": via, "ok": ok, "secs": round(elapsed, 2)}
    with open(TELEMETRY_FILE, "a") as f:
        f.write(json.dumps(rec) + "\n")


# ---- HTTP server -------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, url, method, body=None, headers=None):
        if not url:
            return self._json({"error": "missing ?url="}, 400)
        domain = urlparse(url).netloc
        t0 = time.time()

        # existing vault? try vault-first directly for speed
        entry = load_vault(url)
        first_strategy = "vault_replay" if entry else "direct"
        result, attempts = route(url, method, body, headers) \
            if first_strategy == "direct" else (None, [])
        if result is None:
            r = requests.request(method, url,
                                 headers={"User-Agent": entry["user_agent"],
                                          **(headers or {})},
                                 cookies=entry.get("cookies", {}),
                                 timeout=30,
                                 data=json.dumps(body).encode() if body else None)
            cls = classify(r.status_code, r.text)
            attempts = [{"strategy": "vault_replay", "status": r.status_code, "class": cls}]
            if cls != "none":
                result, more = route(url, method, body, headers)
                attempts += more
            else:
                result = {"status": r.status_code, "class": cls,
                          "via": "vault_replay", "bytes": len(r.content),
                          "head": r.text[:300]}

        ok = result.get("class") == "none"
        elapsed = time.time() - t0
        record_telemetry(domain, result.get("class", "?"),
                         result.get("via", "?"), ok, elapsed)
        self._json({"ok": ok, "elapsed_s": round(elapsed, 2),
                    "attempts": attempts, **result})

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/stats":
            stats = compute_stats()
            return self._json(stats)
        if u.path == "/fetch":
            return self._handle(q.get("url", [""])[0], "GET")
        return self._json({"error": "use /fetch?url= or /stats"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/fetch":
            return self._json({"error": "use POST /fetch"}, 404)
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n) or b"{}")
        self._handle(payload.get("url", ""), payload.get("method", "POST"),
                     payload.get("body"), payload.get("headers"))

    def log_message(self, *a):
        pass


def compute_stats():
    if not TELEMETRY_FILE.exists():
        return {"total": 0}
    by_domain = defaultdict(lambda: [0, 0])   # ok/total
    by_wall = defaultdict(lambda: [0, 0])
    total = 0
    for line in TELEMETRY_FILE.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        total += 1
        by_domain[r["domain"]][1] += 1
        by_wall[r["wall"]][1] += 1
        if r["ok"]:
            by_domain[r["domain"]][0] += 1
            by_wall[r["wall"]][0] += 1
    pct = lambda pair: {"success": round(pair[0]/pair[1]*100, 1) if pair[1] else 0,
                        "total": pair[1]}
    return {"total_requests": total,
            "by_domain": {d: pct(v) for d, v in by_domain.items()},
            "by_wall_class": {w: pct(v) for w, v in by_wall.items()}}


def main():
    port = 8900
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    print(f"[+] GhostPipe ready -> http://127.0.0.1:{port}/fetch?url=ANY_URL")
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.serve_forever()


if __name__ == "__main__":
    main()
