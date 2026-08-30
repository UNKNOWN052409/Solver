"""IPGate Client SDK - tumhare tasks ke liye ready-made wiring.

Gateway (localhost:8899) har wall ko khud handle karta hai; yeh SDK
uske upar high-level helpers deta hai:

    from ipgate_client import InstantProxies

    ip = InstantProxies()
    ip.health()                       # daemon/vault status
    html = ip.home()                  # homepage content (wall-free)
    ip.login(EMAIL, PASSWORD)         # auth dono endpoints pe try
    ip.test_proxy("1.2.3.4:8080")     # unka public proxy-test tool
"""

import json
import time

import requests

GATEWAY = "http://127.0.0.1:8899"
WATCHDOG = "/home/kali/Solver/recon/watchdog.sh"


def ensure_gateway(max_wait=60):
    """Gateway down? Watchdog se revive karo, ready hone tak ruko."""
    import subprocess
    try:
        requests.get(f"{GATEWAY}/status", timeout=3)
        return True
    except Exception:
        pass
    subprocess.run([WATCHDOG], timeout=60)
    for _ in range(max_wait // 4):
        try:
            requests.get(f"{GATEWAY}/status", timeout=3)
            return True
        except Exception:
            time.sleep(4)
    return False
BASE = "https://instantproxies.com"


class IpGateClient:
    """Generic gateway passthrough - kisi bhi site ke liye."""

    def __init__(self, gateway=GATEWAY):
        self.gateway = gateway

    def status(self):
        ensure_gateway()
        return requests.get(f"{self.gateway}/status", timeout=10).json()

    def get(self, url):
        ensure_gateway()
        p = urlparse(url)
        path = p.path + ("?" + p.query if p.query else "")
        return requests.get(f"{self.gateway}/get", params={"path": path}, timeout=60).json()

    def post(self, url, payload):
        ensure_gateway()
        p = urlparse(url)
        path = p.path + ("?" + p.query if p.query else "")
        return requests.post(f"{self.gateway}/post", params={"path": path},
                             json=payload, timeout=60).json()


from urllib.parse import urlparse  # noqa: E402


class InstantProxies(IpGateClient):
    """instantproxies.com specific helpers."""

    AUTH_ENDPOINTS = [
        "/api/auth/sign-in/email",        # Better Auth (primary)
        "/api/authenticate-whmcs",        # WHMCS billing bridge
    ]

    def __init__(self, email=None, password=None):
        super().__init__()
        self.email = email
        self.password = password
        self.session_cookies = {}
        self.auth_result = None

    # ---- content ----

    def home(self):
        r = self.get(f"{BASE}/")
        return {"ok": r.get("ok"), "html_head": r.get("head", ""),
                "bytes": r.get("bytes")}

    # ---- auth ----

    def login(self, email=None, password=None):
        """Dono auth endpoints try karta hai, pehla success returns."""
        email = email or self.email
        password = password or self.password
        results = []
        for ep in self.AUTH_ENDPOINTS:
            r = self.post(f"{BASE}{ep}", {"email": email, "password": password})
            ok = (r.get("status") == 200 and not r.get("challenged"))
            results.append({"endpoint": ep, "http": r.get("status"),
                            "response": r.get("response_head", "")[:200]})
            if ok:
                self.auth_result = r
                break
        self.auth_result = self.auth_result or {"attempts": results}
        return {"ok": any(x["http"] == 200 for x in results), "attempts": results}

    # ---- proxy-test tool (public homepage widget) ----

    def test_proxy(self, target_url):
        """Unka public 'Test Our Proxies' form - best-effort contract."""
        # candidate endpoints; jo 200/non-404 de wahi real hai
        candidates = ["/api/test-proxy", "/api/proxy-test", "/api/check-proxy",
                      "/api/tools/test"]
        for ep in candidates:
            r = self.post(f"{BASE}{ep}", {"target": target_url, "url": target_url})
            if r.get("status") not in (404,):
                return {"endpoint": ep, **r}
        return {"error": "test-endpoint not found - task detail chahiye"}


# ---- CLI ----

if __name__ == "__main__":
    import sys

    ip = InstantProxies(
        email="hacker5566@havenhaus.in",
        password="UnknownR@keshS@g@r@4455",
    )
    gate = IpGateClient()

    cmd = sys.argv[1] if len(sys.argv) > 1 else "health"

    if cmd == "health":
        print(json.dumps(gate.status(), indent=2))
    elif cmd == "home":
        print(json.dumps(ip.home(), indent=2)[:400])
    elif cmd == "login":
        print(json.dumps(ip.login(), indent=2))
    elif cmd == "test":
        target = sys.argv[2] if len(sys.argv) > 2 else "https://cleantalk.org/help/protection-test"
        print(json.dumps(ip.test_proxy(target), indent=2))
    else:
        print("usage: ipgate_client.py [health|home|login|test <url>]")
