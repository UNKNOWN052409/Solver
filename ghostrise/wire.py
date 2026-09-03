"""GhostWire — Solver ka APNA browser engine client.

Playwright zero-dependency: raw CDP (Chrome DevTools Protocol) over
WebSocket. Har protocol message hamara — koi library fingerprint nahi,
na playwright ka known CDP-call-pattern. Stealth ka naya layer:

  - chromium launch (--remote-debugging-port)
  - WS connect -> CDP sessions
  - Page.navigate / evaluate / DOM snapshot
  - GhostMouse raw input injection (Input.dispatchMouseEvent)

Ye engine-layer bhi hamara banata hai — ghostrise orchestration +
GhostWire protocol + GhostMouse input, teeno apne.

Usage:
    from ghostrise.wire import GhostWire
    with GhostWire() as w:                 # chromium launch + attach
        w.goto("https://httpbin.org/get")
        txt = w.text()                     # readable body text
        w.mouse.move(300, 400).click()     # hamara mouse (bezier+RL)
"""
import json
import os
import random
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request

_OS_ENV = {"MOZ_DISABLE_CONTENT_SANDBOX": "1", "DISPLAY": os.environ.get("DISPLAY", "")}


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class GhostWire:
    """Raw CDP client — chromium ko hamare messages se drive karte hain."""

    def __init__(self, headless=True, engine="chromium", profile=None,
                 user_agent=None, extra_args=None):
        self.headless = headless
        self.engine = engine                    # chromium | firefox
        self.profile = profile or tempfile.mkdtemp(prefix="gw-")
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
        self.extra_args = extra_args or []
        self._proc = None
        self._ws = None
        self._msg_id = 0
        self.port = None

    # ---------------------------------------------------------- launch --
    def __enter__(self):
        self.launch()
        return self

    def launch(self):
        if self.engine == "firefox":
            return self._launch_firefox()
        # chromium: playwright cache ya system binary
        cands = [
            os.path.expanduser("~/.cache/ms-playwright/chromium-1234/chrome-linux/chrome"),
            os.path.expanduser("~/.cache/ms-playwright/chromium-1234/chrome-linux/headless_shell"),
            shutil.which("chromium"), shutil.which("chromium-browser"),
        ]
        exe = next((c for c in cands if c and os.path.exists(c)), None)
        if not exe:
            raise RuntimeError("koi chromium binary nahi mili")
        self.port = _free_port()
        args = [
            exe,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile}",
            f"--user-agent={self.user_agent}",
            "--no-first-run", "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars", "--no-sandbox",
        ]
        if self.headless:
            args.append("--headless=new")
        args += self.extra_args
        env = dict(os.environ)
        env.update(_OS_ENV)
        self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, env=env)
        # DevTools ws endpoint discover
        ws_url = self._wait_devtools()
        self._connect(ws_url)
        return self

    def _launch_firefox(self):
        # Firefox ka remote protocol (RDP) CDP nahi hai — ye path mara,
        # chromium hi raw-CDP engine hai. Firefox launch sirf fallback
        # read-fetch ke liye (pure HTTP) — wire engine chromium hai.
        raise RuntimeError("firefox raw-wire nahi — engine='chromium' use karo "
                           "(Firefox playwright-ladder me rehta hai)")

    def _wait_devtools(self, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.port}/json/version", timeout=2) as r:
                    return json.loads(r.read().decode()).get("webSocketDebuggerUrl")
            except Exception:
                time.sleep(0.4)
        raise RuntimeError("devtools endpoint nahi mila")

    # --------------------------------------------------------- protocol --
    def _connect(self, ws_url):
        import websockets.sync.client as wsc
        self._ws = wsc.connect(ws_url)

    def _send(self, method, params=None, session_id=None):
        self._msg_id += 1
        msg = {"id": self._msg_id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        self._ws.send(json.dumps(msg))
        while True:
            raw = json.loads(self._ws.recv(timeout=30))
            if raw.get("id") == self._msg_id:
                if "error" in raw:
                    raise RuntimeError(f"CDP {method}: {raw['error']}")
                return raw.get("result", {})

    # ------------------------------------------------------------- page --
    def _target(self):
        """Pehla page target — attach karke session id."""
        tg = self._send("Target.getTargets")
        for t in tg.get("targetInfos", []):
            if t.get("type") == "page":
                s = self._send("Target.attachToTarget",
                               {"targetId": t["targetId"], "flatten": True})
                return s["sessionId"]
        raise RuntimeError("koi page target nahi")

    def goto(self, url, timeout=45000):
        sid = self._sid = getattr(self, "_sid", None) or self._target()
        # stealth patches pehle: navigator.webdriver hatao
        self._send("Page.enable", session_id=sid)
        try:
            self._send("Page.addScriptToEvaluateOnNewDocument", {
                "source": (
                    "Object.defineProperty(navigator,'webdriver',"
                    "{get:()=>undefined});"
                    "window.chrome={runtime:{}};"
                    "Object.defineProperty(navigator,'languages',"
                    "{get:()=>['en-US','en']});"
                    "Object.defineProperty(navigator,'plugins',"
                    "{get:()=>[1,2,3,4,5]});"
                )}, session_id=sid)
        except Exception:
            pass
        self._send("Page.navigate", {"url": url}, session_id=sid)
        deadline = time.time() + timeout / 1000
        while time.time() < deadline:
            st = self._send("Runtime.evaluate",
                           {"expression": "document.readyState"},
                           session_id=sid)
            if st.get("result", {}).get("value") in ("complete", "interactive"):
                return self
            time.sleep(0.3)
        return self

    def evaluate(self, expr):
        sid = getattr(self, "_sid", None) or self._target()
        r = self._send("Runtime.evaluate",
                       {"expression": expr, "returnByValue": True},
                       session_id=sid)
        return r.get("result", {}).get("value")

    def text(self, limit=4000):
        return (self.evaluate("document.body ? document.body.innerText.slice(0,%d) : ''" % limit)) or ""

    # ------------------------------------------------------- hamara mouse --
    @property
    def mouse(self):
        from ghostrise.wire_mouse import WireMouse
        if not hasattr(self, "_mouse"):
            self._mouse = WireMouse(self)
        return self._mouse

    # ------------------------------------------------------------- close --
    def close(self):
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        try:
            shutil.rmtree(self.profile, ignore_errors=True)
        except Exception:
            pass

    def __exit__(self, *exc):
        self.close()
