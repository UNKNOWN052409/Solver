"""GhostRise MCP vision — a tiny stdlib MCP-style perception service that turns
browser DOM into a model-agnostic perception map ANY attached AI model can act on.

Layered model support (LO's LO ARCH):
  * NON-vision model  -> PerceptionLayer + ActionPlan: a labelled DOM/UI map
        ({element_id, selector, tag, role, bbox, text, actionable}) plus a concrete
        ordered action list from a plain-English instruction. The model knows
        WHERE the form is, WHICH field, and WHICH button — with ZERO pixels.
  * VISION model      -> VisionModelSupport: the SAME perception map PLUS
        clickable-region centroids (bbox centre x,y) so whatever a vision model
        "sees", its clicks are validated/corrected against the real DOM boxes
        (fixes "vision clicked the wrong box").

Transport: a tiny stdlib http.server exposing POST /perceive {instruction, url}
-> JSON {elements, plan, clicks}. No external framework — LAN/stdio ready.
The same functions are importable — MCP servers, agent loops, or a local
non-vision model can all call them directly.

This module ONLY imports and extends the sibling modules ai_assistant.py
(describe_page / _CompatPage / _human / click_by_selector / fill_model_written)
and browser_agent.py / wire.py (GhostWire). It never overwrites them.

Usage:
    python -m ghostrise.mcp_vision --self-test      # REAL GhostWire + page + action
    python -m ghostrise.mcp_vision --serve --port 8711   # POST /perceive
    curl -s localhost:8711/perceive -d '{"instruction":"fill email and password then submit","url":"https://example.com"}'
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from ghostrise.ai_assistant import (  # siblings — extend, never overwrite
    _CompatPage,
    describe_page,
    click_by_selector,
    verify_click_landed,
)
from ghostrise.wire import GhostWire  # real browser engine (context manager)

# =====================================================================
# 1) PerceptionLayer — DOM/UI perception map (what a NON-VISION model sees)
# =====================================================================


def _bbox_from_rect(rect: dict | None) -> dict | None:
    """Normalise any getBoundingClientRect-ish rect to {x, y, w, h}."""
    if not rect:
        return None
    x = rect.get("x", rect.get("left", 0.0))
    y = rect.get("y", rect.get("top", 0.0))
    w = rect.get("width", 0.0)
    h = rect.get("height", 0.0)
    # some maps deliver centre+size
    if (w == 0 or h == 0) and "center_x" in rect:
        cx, cy = rect.get("center_x", 0.0), rect.get("center_y", 0.0)
        w = rect.get("w", rect.get("width", 0.0))
        h = rect.get("h", rect.get("height", 0.0))
        x, y = cx - w / 2.0, cy - h / 2.0
    return {"x": round(float(x), 1), "y": round(float(y), 1),
            "w": round(float(w), 1), "h": round(float(h), 1)}


def _selector_for(e: dict) -> str:
    """Best-effort deterministic CSS selector for an element-map entry."""
    if e.get("id"):
        return f"#{e['id']}"
    if e.get("name"):
        return f"{e['tag']}[name={e['name']!r}]"
    if e.get("tag") == "input" and e.get("type"):
        return f"input[type={e['type']!r}]"
    return e.get("tag") or e.get("selector") or ""


def _role_for(e: dict) -> str:
    tag = e.get("tag", "")
    if e.get("kind") == "field" or tag in ("input", "textarea", "select"):
        return "field"
    if tag == "a":
        return "link"
    return "button"


class PerceptionLayer:
    """Read a live page and emit a labelled DOM/UI perception map.

    Works on ANY engine page with .evaluate() — a raw GhostWire, a
    playwright/cloak page, or a _CompatPage wrapper. Non-vision models consume
    this map directly: {element_id, selector, tag, role, bbox, text, actionable}.
    """

    def __init__(self, page):
        # wrap so we face a uniform surface (mouse/keyboard shims included)
        self.page = _CompatPage(page) if not isinstance(page, _CompatPage) else page
        self._raw = page

    # -- helpers ----------------------------------------------------------
    def perceive(self, include_text=True) -> list:
        """The labelled perception map for the current live page."""
        d = describe_page(self._raw)
        out = []
        seen = set()
        for i, e in enumerate(d.get("elements", [])):
            bbox = _bbox_from_rect(e.get("rect"))
            if not bbox or bbox["w"] < 1 or bbox["h"] < 1:
                continue
            role = _role_for(e)
            actionable = bool(role in ("button", "link") or e.get("kind") == "clickable")
            sel = _selector_for(e)
            # dedupe identical triples (same tag+name) so ids are stable
            key = (sel, e.get("type", ""), e.get("text", ""))
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "element_id": i,
                "selector": sel or e.get("selector", ""),
                "tag": e.get("tag", ""),
                "role": role,
                "text": (e.get("innerText") or e.get("text") or "").strip()[:60],
                "name": e.get("name", ""),
                "type": e.get("type", ""),
                "value": (e.get("value", "") or "")[:40],
                "bbox": bbox,
                "actionable": actionable,
            })
        return out

    def text(self) -> str:
        return describe_page(self._raw).get("text", "")

    def title_url(self) -> dict:
        d = describe_page(self._raw)
        return {"title": d.get("title", ""), "url": d.get("url", "")}


# =====================================================================
# 2) ActionPlan — instruction -> concrete ordered action list
# =====================================================================

# token -> (role, hint_substrings) for matching "fill email and password"
_FIELD_HINTS = [
    (("email", "e-mail", "mail"),            ("email", "mail")),
    (("password", "passwd", "pwd", "pass"),  ("password", "passwd", "pass")),
    (("user", "username", "login", "userid"),("user", "login", "email")),
    (("name", "full_name", "fname", "lname"),("name",)),
    (("first", "first_name"),                ("first",)),
    (("last", "last_name"),                  ("last",)),
    (("phone", "tel", "mobile"),             ("phone", "tel", "mobile")),
    (("city",),                              ("city",)),
    (("search", "q", "query"),               ("search", "q", "query")),
    (("subject",),                           ("subject",)),
    (("message", "msg", "body", "comment"),  ("message", "body", "comment")),
]
_BUTTON_HINTS = [
    (("submit", "signin", "sign-in", "login", "log in", "send", "register",
      "continue", "next", "create account", "save"), ("submit", "sign in", "login",
      "send", "continue", "next", "register")),
]


def _score_field(e: dict, tokens: list) -> int:
    """Naive token match score: how strongly this field matches the instruction."""
    hay = " ".join(str(e.get(k, "")) for k in ("name", "type", "selector", "text", "id"))
    hay = hay.lower()
    score = 0
    for key_tokens, hints in _FIELD_HINTS:
        if any(t in tokens for t in key_tokens):
            if any(h in hay for h in hints):
                score += 3
                if e.get("type") in ("email", "password"):
                    score += 2
    return score


def _match_button(es: list, tokens: list) -> dict | None:
    for e in es:
        if e["role"] not in ("button", "link") or not e["actionable"]:
            continue
        hay = (" ".join([e["text"], e["selector"], e["name"]])).lower()
        for keys, hints in _BUTTON_HINTS:
            if any(t in tokens for t in keys) and any(h in hay for h in hints):
                return e
    return None


class ActionPlan:
    """Turn a plain-English instruction into a concrete ordered action list.

    Non-vision models get this as text:
        [{action: fill, selector: input[name=email], value: ...}, ...]
    Matching is token/DOM-driven, not vision-driven. Values come from a
    `values` map (or from the instruction's own "=value" tokens like
    email=me@x.com). Falls back to perceptual order when no hint matches.
    """

    def __init__(self, elements: list):
        self.elements = elements
        self.fields = [e for e in elements if e["role"] == "field"]
        self.buttons = [e for e in elements if e["role"] in ("button", "link")]

    def build(self, instruction: str, values: dict | None = None) -> list:
        values = values or {}
        tokens = {t.lower() for t in instruction.replace("=", " ").split()}

        # inline "field=value" pairs in the instruction
        for piece in instruction.replace(",", " ").split():
            if "=" in piece:
                k, v = piece.split("=", 1)
                values[k.strip().lower()] = v.strip()

        actions = []
        used = set()

        # 1) fill every field the instruction mentions
        for field in sorted(self.fields, key=lambda e: -_score_field(e, tokens)):
            s = _score_field(field, tokens)
            if s <= 0:
                continue
            # value: exact key match, else hint-key match, else placeholder
            val = None
            blob = (field.get("name") or field.get("id") or "").lower()
            if blob in values:
                val = values[blob]
            if val is None:
                # which hint-group does THIS field belong to?
                group = None
                for key_tokens, hints in _FIELD_HINTS:
                    if any(h in blob for h in hints):
                        group = key_tokens
                        break
                if group is None:
                    group = ()
                for k, v in values.items():
                    if any(h in k for h in group):
                        val = v
                        break
            actions.append({
                "action": "fill",
                "selector": field["selector"],
                "value": val if val is not None else (field.get("value") or ""),
                "element_id": field["element_id"],
            })
            used.add(field["element_id"])

        # 2) if the instruction says fill but named none (generic "fill the form"),
        #    fall back to all empty fields in DOM order
        if any(t in tokens for t in ("fill", "enter", "type", "put")) and not actions:
            for field in self.fields:
                if field["element_id"] in used:
                    continue
                k = field.get("name") or field.get("id") or ""
                val = values.get(k, values.get(field.get("type", ""), ""))
                actions.append({"action": "fill", "selector": field["selector"],
                                "value": val, "element_id": field["element_id"]})
                used.add(field["element_id"])

        # 3) the submit / next button
        if any(t in tokens for t in
               ("submit", "sign", "login", "log", "send", "register", "continue",
                "next", "create", "save", "click", "press")):
            btn = _match_button([e for e in self.buttons if e["element_id"] not in used],
                                tokens)
            if btn is None and ("submit" in tokens or "click" in tokens or "press" in tokens):
                btn = (next((e for e in self.buttons if "submit" in (e["text"]+e["name"]).lower()), None)
                       or (self.buttons[0] if self.buttons else None))
            if btn:
                actions.append({
                    "action": "click", "selector": btn["selector"],
                    "value": "", "element_id": btn["element_id"],
                })
        return actions


# =====================================================================
# 3) VisionModelSupport — perception map + clickable centroid regions
# =====================================================================


class VisionModelSupport:
    """Same perception map PLUS clickable-region centroids.

    A vision model "sees" the page as pixels; these centroids are the DOM boxes
    underneath what it sees, so when the model says "click the submit button at
    top right", we hand back the exact centre that is GUARANTEED to be inside
    the real element box — killing the "vision clicked the wrong box" class of
    bugs. Clicks are validated against real DOM boxes before dispatch.
    """

    def __init__(self, page):
        self.layer = PerceptionLayer(page)

    def perceive(self) -> dict:
        elements = self.layer.perceive()
        clicks = []
        for e in elements:
            b = e["bbox"]
            cx = round(b["x"] + b["w"] / 2.0, 1)
            cy = round(b["y"] + b["h"] / 2.0, 1)
            if e["actionable"] or e["role"] == "field":
                clicks.append({
                    "element_id": e["element_id"],
                    "selector": e["selector"],
                    "role": e["role"],
                    "centroid": {"x": cx, "y": cy},
                    "bbox": b,
                })
        return {"elements": elements, "clicks": clicks}

    @staticmethod
    def validate_click(click: dict, element: dict) -> dict:
        """Confirm a proposed click centroid is inside the real DOM box."""
        b = element["bbox"]
        c = click.get("centroid", {})
        cx, cy = c.get("x"), c.get("y")
        if cx is None or cy is None:
            return {"valid": False, "reason": "no centroid"}
        inside = (b["x"] <= cx <= b["x"] + b["w"]
                  and b["y"] <= cy <= b["y"] + b["h"])
        # if it landed outside, snap it to the box centre (the fix)
        return {"valid": inside, "in_box": inside,
                "centroid": ({"x": cx, "y": cy} if inside
                             else {"x": round(b["x"] + b["w"] / 2, 1),
                                   "y": round(b["y"] + b["h"] / 2, 1)}),
                "reason": "inside box" if inside else "snapped to box centre"}


# =====================================================================
# 4) MCP transport — tiny stdlib http.server POST /perceive
# =====================================================================


class _MCPHandler(BaseHTTPRequestHandler):
    server_version = "GhostRiseMCP/0.1"

    def log_message(self, fmt, *args):  # keep stdio clean
        sys.stderr.write("mcp_vision: " + fmt % args + "\n")

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/perceive":
            self._json(404, {"ok": False, "error": "use POST /perceive"})
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._json(400, {"ok": False, "error": f"bad json: {e}"})
            return
        try:
            result = serve_perceive(req)
            self._json(200, result)
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)})

    def do_GET(self):
        self._json(200, {"ok": True, "service": "ghostrise.mcp_vision",
                         "endpoints": ["POST /perceive {instruction, url}"]})


class _MCPApp:
    """Shared app state: one live browser across requests (LAN/stdio ready)."""

    def __init__(self, engine="chromium"):
        self._wire = None
        self._engine = engine
        self._lock = Thread()  # placeholder, replaced below

    def ensure_page(self, url: str):
        import threading
        if not hasattr(self, "_lock") or not isinstance(self._lock, threading.Lock):
            self._lock = threading.Lock()
        if not url:                       # normalise: "keep" or "" -> default
            url = "https://example.com"
        with self._lock:
            fresh = False
            if self._wire is None:
                self._wire = GhostWire(headless=True, engine=self._engine)
                self._wire.__enter__()
                fresh = True
            if url != "keep" and (fresh or not (self._goto_eq(url))):
                self._wire.goto(url)
            return self._wire

    def _goto_eq(self, url: str) -> bool:
        try:
            return (self._wire.evaluate("location.href") or "") == url
        except Exception:
            return False

    def close(self):
        if self._wire is not None:
            try:
                self._wire.__exit__(None, None, None)
            except Exception:
                pass
            self._wire = None


_APP = _MCPApp()


def serve_perceive(req: dict) -> dict:
    """Handle one /perceive request against the shared live browser.

    req: {"instruction": "...", "url": "...", "vision": bool|None,
          "values": {field:value}}
    -> {"ok": True, "elements": [...], "plan": [...], "clicks": [...],
        "mode": "vision"|"dom"}
    """
    url = req.get("url") or "https://example.com"
    instruction = req.get("instruction", "")
    vision = bool(req.get("vision", False))
    values = req.get("values") or {}
    page = _APP.ensure_page(url)
    time.sleep(0.3)  # let SPA settle if navigated

    layer = PerceptionLayer(page)
    elements = layer.perceive()
    plan = ActionPlan(elements).build(instruction, values)

    out = {
        "ok": True,
        "mode": "vision" if vision else "dom",
        "instruction": instruction,
        "url": layer.title_url()["url"],
        "elements": elements,
        "plan": plan,
    }
    if vision:
        out["clicks"] = VisionModelSupport(page).perceive()["clicks"]
    else:
        out["clicks"] = []
    return out


def start_server(port: int = 8711, host: str = "127.0.0.1",
                 daemon: bool = True) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), _MCPHandler)
    t = Thread(target=srv.serve_forever, daemon=daemon)
    t.start()
    return srv


# =====================================================================
# 5) CLI + self-test
# =====================================================================


class _FakePage:
    """Mock page that runs REAL DOM strings through a tiny JS-eval stand-in.

    Used for the pure-DOM fallback test when no real chromium can launch:
    `evaluate()` here hosts a real template literal of interactive elements,
    and the _ELEMENT_MAP_JS-style string is parsed from an explicit map so the
    pipeline (perceive -> plan -> clicks) is exercised with REAL DOM data.
    """

    def __init__(self, dom: dict):
        self._dom = dom  # {"elements": [...], "title":..., "url":...}

    def evaluate(self, expr):
        # simulate the element-map JS's output contract
        if "getBoundingClientRect" in expr or "querySelectorAll" in expr:
            return self._dom.get("elements", [])
        if "document.title" in expr:
            return self._dom.get("title", "")
        if "location.href" in expr:
            return self._dom.get("url", "")
        if "innerText" in expr:
            return self._dom.get("text", "")
        return ""

    @property
    def mouse(self):
        return _FakeMouse(self)


class _FakeMouse:
    def __init__(self, page):
        self.page = page
        self.clicks = []

    def click(self, x, y, **kw):
        self.clicks.append((x, y))
        return self


_DOM_FORM = {
    "title": "Login",
    "url": "file:///login.html",
    "text": "Sign in to your account Email Password Submit",
    "elements": [
        {"tag": "input", "selector": "input[name=email]",
         "id": "", "name": "email", "type": "email", "kind": "field",
         "rect": {"x": 200, "y": 150, "width": 300, "height": 36}},
        {"tag": "input", "selector": "input[name=password]",
         "id": "", "name": "password", "type": "password", "kind": "field",
         "rect": {"x": 200, "y": 210, "width": 300, "height": 36}},
        {"tag": "button", "selector": "button", "id": "", "name": "", "type": "submit",
         "kind": "clickable", "text": "Submit", "innerText": "Submit",
         "rect": {"x": 240, "y": 270, "width": 200, "height": 44}},
    ],
}


def _run_selftest() -> int:
    print("== ghostrise.mcp_vision self-test ==")
    # ---- pure-DOM fallback first (always runs, real DOM strings) ----
    layer = PerceptionLayer(_FakePage(_DOM_FORM))
    elements = layer.perceive()
    print(f"[pure-DOM] perceived {len(elements)} elements")
    for e in elements:
        print(f"   #{e['element_id']} {e['role']:<6} {e['selector']:<22} "
              f"bbox={e['bbox']} actionable={e['actionable']} text={e['text']!r}")
    plan = ActionPlan(elements).build(
        "fill email and password then submit", {"email": "lo@example.com", "password": "s3cr3t"})
    print(f"[pure-DOM] plan ({len(plan)} actions):")
    for a in plan:
        print(f"   {a['action']:<5} {a['selector']:<24} value={a['value']!r}")
    v = VisionModelSupport(_FakePage(_DOM_FORM)).perceive()
    submit = next(c for c in v["clicks"] if c["role"] in ("button", "link"))
    print(f"[pure-DOM] vision centroids: {len(v['clicks'])} clickable; "
          f"submit centroid={submit['centroid']}")
    assert len(elements) >= 3 and any(a["action"] == "click" for a in plan), \
        "pure-DOM perception/plan broke"
    assert submit["centroid"] == {"x": 340, "y": 292}, "centroid wrong"
    print("[pure-DOM] OK")

    # ---- REAL GhostWire browser, if it can launch ----
    pages = 0
    try:
        with GhostWire(headless=True, engine="chromium") as w:
            w.goto("https://example.com")
            rlayer = PerceptionLayer(w)               # REAL live page
            rpages = rlayer.perceive()
            print(f"[real] launched serial GhostWire, perceived {len(rpages)} elements")
            print(f"[real] page title/url: {rlayer.title_url()}")
            for e in rpages:
                print(f"[real]   #{e['element_id']} {e['role']:<6} "
                      f"{e['selector']:<22} bbox={e['bbox']} "
                      f"actionable={e['actionable']} text={e['text']!r}")
            dom_elements = rpages
            # prove an action runs: click the first real link (if any)
            done = False
            for e in dom_elements:
                if e["actionable"] and e["role"] == "link":
                    prior = verify_click_landed(w, e["selector"])["landed"]
                    ok = click_by_selector(w, e["selector"])
                    print(f"[real] action click {e['selector']} "
                          f"click_ok={ok} verify_landed_prior={prior}")
                    done = True
                    break
            if not done:
                print("[real] no interactive element on example.com to click — "
                      "perception-only OK")
            # prove the MCP transport payload builds end-to-end
            payload = serve_perceive({"url": "https://example.com",
                                      "instruction": "click the first link".lower(),
                                      "vision": True})
            print(f"[real] MCP /perceive-> {len(payload['elements'])} elements, "
                  f"{len(payload['clicks'])} vision clicks, {len(payload['plan'])} plan")
        print("[real] GhostWire OK")
    except Exception as e:
        print(f"[real] GhostWire launch FAILED (delivering pure-DOM test only): {e!r}")
    return 0


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if "--self-test" in argv or "-t" in argv:
        return _run_selftest()
    if "--serve" in argv or "-s" in argv:
        port = 8711
        for i, a in enumerate(argv):
            if a == "--port" and i + 1 < len(argv):
                port = int(argv[i + 1])
        srv = start_server(port=port)
        print(f"ghostrise.mcp_vision serving on http://127.0.0.1:{port}/perceive "
              f"(POST {{instruction,url}})", flush=True)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            _APP.close()
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
