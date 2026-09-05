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


class _WireFrame:
    """Playwright-frame-lite: url + tiny locator-surface for captcha_agent."""

    def __init__(self, wire, frame_id, url):
        self.wire = wire
        self.frame_id = frame_id
        self.url = url or ""

    def evaluate(self, expr):
        return self.wire.frame_eval(self.frame_id, expr)

    @property
    def content(self):
        return self.wire.frame_eval(self.frame_id, "document.documentElement.outerHTML") or ""

    def locator(self, sel):
        """Playwright-lite locator: JS querySelector — evaluate/rects."""
        return _WireLocator(self.wire, self.frame_id, sel)

    def wait_for_selector(self, sel, timeout=4000):
        """Playwright-lite: selector ka wait — mila to _WireLocator."""
        import time as _t
        deadline = _t.time() + timeout / 1000
        while _t.time() < deadline:
            if self.locator(sel).count() > 0:
                return _WireHandle(self.wire, self.frame_id, sel)
            _t.sleep(0.25)
        return None

    def find_and_click(self, sel, human=None):
        """JS se element locate -> absolute coords -> WireMouse click."""
        import json as _j
        pos_json = self.wire.frame_eval(
            self.frame_id,
            "JSON.stringify((function(){var e=document.querySelector(%r);"
            "if(!e)return null;var r=e.getBoundingClientRect();"
            "return {x:r.x+window.scrollX,y:r.y+window.scrollY,width:r.width,height:r.height};})())" % sel,
        )
        if not pos_json:
            return False
        try:
            pos = _j.loads(pos_json)
        except Exception:
            return False
        # iframe ka offset add karo — frame_eval me coords frame-local
        off = self.wire.frame_offset(self.frame_id)
        cx = pos["x"] + pos["width"] / 2 + off[0]
        cy = pos["y"] + pos["height"] / 2 + off[1]
        m = human or self.wire.mouse
        m.click(cx, cy)
        return True


class _WireHandle:
    """ElementHandle-lite: bounding_box + content_frame + click-coords."""

    def __init__(self, wire, frame_id, sel):
        self.wire = wire
        self.frame_id = frame_id
        self.sel = sel

    def bounding_box(self):
        import json as _j
        raw = self.wire.frame_eval(
            self.frame_id,
            "JSON.stringify((function(){var e=document.querySelector(%r);"
            "if(!e)return null;var r=e.getBoundingClientRect();"
            "return {x:r.x+window.scrollX,y:r.y+window.scrollY,"
            "width:r.width,height:r.height};})())" % self.sel,
        )
        if not raw:
            return None
        try:
            return _j.loads(raw)
        except Exception:
            return None

    def content_frame(self):
        """iframe-element ho to uska _WireFrame URL-match se."""
        # nested frame dhoondo: page.frames me jiska parent ye frame hai
        # wire me parent-info nahi — URL se: iframe ka src attribute
        src = self.wire.frame_eval(
            self.frame_id,
            "document.querySelector(%r)?.getAttribute('src') || ''" % self.sel,
        )
        if not src:
            return None
        base = src.split("?")[0]
        for f in self.wire.frames():
            if f.url and f.url.split("?")[0].startswith(base[:50]):
                return f
        return None


class _WireLocator:
    """querySelector-lite: evaluate/inner_text/rects — captcha-agent surface.

    Playwright-chain compatible: .first.inner_text(), .nth(i), .count()."""

    def __init__(self, wire, frame_id, sel):
        self.wire = wire
        self.frame_id = frame_id
        self.sel = sel

    @property
    def first(self):
        return self  # single-elem semantics — inner_text pehle elem ka

    def nth(self, i):
        return _WireLocatorIndexed(self.wire, self.frame_id, self.sel, i)

    def _base_sel(self):
        return self.sel

    def inner_text(self, timeout=2000):
        return (
            self.wire.frame_eval(
                self.frame_id,
                "document.querySelector(%r)?.innerText || ''" % self._base_sel(),
            )
            or ""
        )

    def get_attribute(self, name, timeout=1500):
        return self.wire.frame_eval(
            self.frame_id,
            "document.querySelector(%r)?.getAttribute(%r) || null" % (self._base_sel(), name),
        )

    def locator(self, sel):
        return _WireLocator(self.wire, self.frame_id, f"{self.sel} {sel}")

    def click(self, human=None):
        """JS-rect -> absolute coords -> WireMouse click."""
        import json as _j
        raw = self.wire.frame_eval(
            self.frame_id,
            "JSON.stringify((function(){var e=document.querySelector(%r);"
            "if(!e)return null;var r=e.getBoundingClientRect();"
            "return {x:r.x+window.scrollX,y:r.y+window.scrollY,width:r.width,height:r.height};})())"
            % self._base_sel(),
        )
        if not raw:
            return False
        try:
            pos = _j.loads(raw)
        except Exception:
            return False
        off = self.wire.frame_offset(self.frame_id)
        cx = pos["x"] + pos["width"] / 2 + off[0]
        cy = pos["y"] + pos["height"] / 2 + off[1]
        m = human or self.wire.mouse
        m.click(cx, cy)
        return True

    def evaluate(self, expr):
        return self.wire.frame_eval(self.frame_id, expr)

    def inner_text(self, timeout=2000):
        return (
            self.wire.frame_eval(
                self.frame_id,
                "document.querySelector(%r)?.innerText || ''" % self.sel,
            )
            or ""
        )

    def count(self):
        v = self.wire.frame_eval(
            self.frame_id, "document.querySelectorAll(%r).length" % self.sel
        )
        return int(v or 0)


class _WireLocatorIndexed(_WireLocator):
    """nth(i) — querySelectorAll[i] (playwright nth semantics)."""

    def __init__(self, wire, frame_id, sel, idx):
        super().__init__(wire, frame_id, sel)
        self.idx = idx

    def _base_sel(self):
        return self.sel  # JS me index-apply hota hai

    def _js_get(self, expr_fmt):
        """expr_fmt me %s = selector, %d = index — JS wrapper."""
        return self.wire.frame_eval(
            self.frame_id,
            expr_fmt % (self.sel, self.idx),
        )

    def inner_text(self, timeout=2000):
        return (
            self._js_get(
                "var l=document.querySelectorAll(%r);l[%d]?l[%d].innerText:''"
            )
            or ""
        )

    def get_attribute(self, name, timeout=1500):
        # selector+index+attr — 3-arg format
        return self.wire.frame_eval(
            self.frame_id,
            "var l=document.querySelectorAll(%r);l[%d]?l[%d].getAttribute(%r):null"
            % (self.sel, self.idx, self.idx, name),
        )

    def count(self):
        v = self.wire.frame_eval(
            self.frame_id, "document.querySelectorAll(%r).length" % self.sel
        )
        return int(v or 0)

    def _rect(self):
        import json as _j
        raw = self.wire.frame_eval(
            self.frame_id,
            "JSON.stringify((function(){var l=document.querySelectorAll(%r);"
            "var e=l[%d];if(!e)return null;var r=e.getBoundingClientRect();"
            "return {x:r.x+window.scrollX,y:r.y+window.scrollY,width:r.width,height:r.height};})())"
            % (self.sel, self.idx),
        )
        if not raw:
            return None
        try:
            return _j.loads(raw)
        except Exception:
            return None

    def bounding_box(self):
        return self._rect()

    def rects(self):
        r = self._rect()
        return [r] if r else []

    def click(self, human=None):
        pos = self._rect()
        if not pos:
            return False
        off = self.wire.frame_offset(self.frame_id)
        m = human or self.wire.mouse
        m.click(pos["x"] + pos["width"] / 2 + off[0], pos["y"] + pos["height"] / 2 + off[1])
        return True

    def rects(self):
        import json as _j
        raw = self.wire.frame_eval(
            self.frame_id,
            "JSON.stringify(Array.from(document.querySelectorAll(%r))"
            ".map(function(e){var r=e.getBoundingClientRect();"
            "return {x:r.x+window.scrollX,y:r.y+window.scrollY,width:r.width,height:r.height};}))" % self.sel,
        )
        if not raw:
            return []
        try:
            return _j.loads(raw)
        except Exception:
            return []

    def get_attribute(self, name, timeout=1500):
        return self.wire.frame_eval(
            self.frame_id,
            "document.querySelector(%r)?.getAttribute(%r) || null" % (self.sel, name),
        )


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

    # ------------------------------------------------------------- frames --
    def main_locator(self, sel):
        """Main-page querySelector-lite locator (HumanActions surface)."""
        return _WireLocator(self, self._sid_frame(), sel)

    def _sid_frame(self):
        """Main frame id — frames()[0] ya 'main'."""
        try:
            fs = self.frames()
            if fs:
                return fs[0].frame_id
        except Exception:
            pass
        return ""

    def frames(self):
        """Frame-tree flatten: page + iframes ka (id, url) list."""
        try:
            tree = self._send("Page.getFrameTree", session_id=self._sid)
            out = []

            def walk(node):
                f = node.get("frame", {})
                out.append((f.get("id", ""), f.get("url", "")))
                for c in node.get("childFrames", []):
                    walk(c)

            walk(tree.get("frameTree", {}))
            # _WireFrame objects — locator-lite chahiye captcha_agent ko
            return [_WireFrame(self, fid, url) for fid, url in out]
        except Exception:
            return []

    def frame_eval(self, frame_id, expr):
        """Frame-specific eval — har frame ka apna context hota hai."""
        # executionContexts enumerate karke frame ka context dhoondo
        try:
            ctxs = self._send("Runtime.enable", session_id=self._sid)
            # events me contexts aate hain — enable ke baad poll
            import time as _t
            _t.sleep(0.2)
            # simplest robust route: Page.createIsolatedWorld per-frame
            r = self._send(
                "Page.createIsolatedWorld",
                {"frameId": frame_id, "worldName": "ghost-wire-frame"},
                session_id=self._sid,
            )
            cid = r.get("executionContextId")
            if cid is None:
                return None
            ev = self._send(
                "Runtime.evaluate",
                {"expression": expr, "contextId": cid, "returnByValue": True},
                session_id=self._sid,
            )
            return ev.get("result", {}).get("value")
        except Exception:
            return None

    def frame_offset(self, frame_id, frame_url=""):
        """Frame ka page-offset: parent-page iframe rects se URL-match.

        Cross-origin frames me window.frameElement blocked hota hai —
        isliye parent page pe iframes enumerate karke matching URL ka
        rect use karte hain (hCaptcha pattern pe reliable)."""
        import json as _j
        # frame ka URL nikaalo agar diya nahi
        if not frame_url:
            for f in self.frames():
                if f.frame_id == frame_id:
                    frame_url = f.url
                    break
        if not frame_url or frame_url == self.evaluate("location.href"):
            return [0.0, 0.0]
        raw = self.evaluate(
            "JSON.stringify(Array.from(document.querySelectorAll('iframe'))"
            ".map(function(f){var r=f.getBoundingClientRect();"
            "return {src:f.src||'',x:r.x+window.scrollX,y:r.y+window.scrollY,"
            "width:r.width,height:r.height};}))"
        )
        if not raw:
            return [0.0, 0.0]
        try:
            iframes = _j.loads(raw)
        except Exception:
            return [0.0, 0.0]
        # URL prefix-match (query/hash differences ignore)
        base = frame_url.split("?")[0]
        for f in iframes:
            if f["src"] and base.startswith(f["src"].split("?")[0][:60]):
                return [f["x"], f["y"]]
        return [0.0, 0.0]

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
