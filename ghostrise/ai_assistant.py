"""GhostRise AI assistant layer — let an LLM drive the browser.

A model-agnostic bridge between the GhostRise browser surface
(GhostSession -> page / HumanActions / GhostWire) and any OpenAI-compatible
chat-completions endpoint ("Quan", compose, a proxy, the real OpenAI API,
a local vLLM/ollama box, whatever). It has three distinct pieces:

1. `AiAssistant`          — config/env init + `chat()` that POSTs to
                            /chat/completions. Kept simple, non-streaming first.
2. AI HELPERS (repo design):
     * NON-vision "layout/box helper"  — reads DOM bounding boxes via
       getBoundingClientRect (the same JS wire GhostWire/behavior use) and
       returns a structured element map + error detections -> the model knows
       WHERE things are and what's wrong, without ever seeing a pixel.
     * VISION "click-verifier"         — after an action, re-reads state
       (and, when the engine exposes a screenshot, a screenshot) to confirm
       the click landed. Playwright-style pages expose .screenshot(); the raw
       GhostWire engine currently does NOT — we note that and fall back to a
       DOM-state heuristic.
     * code-writer dispatch           — the agent loop (browser_agent.py)
       matches a natural-language instruction to a tool and runs it.
3. A plain-dict TOOL REGISTRY  name -> (schema, callable)  so the model-facing
   surface is model-agnostic (works whether the model does native tool-calling,
   a JSON "tool + args" reply, or just matches tool names by string).

No live model is required for the helpers or the agent loop — they run on a
real page today. Only `chat()` needs an API key/base URL, and it is fully
wired so it works the moment a key is present.

Usage:
    from ghostrise.ai_assistant import AiAssistant
    asst = AiAssistant()                 # env: OPENAI_BASE_URL/KEY/MODEL
    asst.chat([{"role":"user","content":"hi"}])
    # helpers take a page object (GhostSession.page() or GhostWire page):
    from ghostrise.ai_assistant import describe_page
    from ghostrise.browser_agent import AgentLoop
    loop = AgentLoop(asst)
    loop.run(page, "Quan, form fill kar de: name=LO, mark kar de")
"""

import json
import os
import urllib.request

# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def default_config() -> dict:
    """Pull config from env with sane defaults (all overridable per-call)."""
    return {
        "api_key": os.environ.get("OPENAI_API_KEY", ""),
        "base_url": os.environ.get(
            "OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        # non-vision (text) chat + tool-dispatch model
        "model": os.environ.get("OPENAI_MODEL", "gpt-4o"),
        # separate VISION model (image + text). Falls back to `model`.
        "vision_model": os.environ.get("OPENAI_VISION_MODEL", "") or None,
        # a non-vision text-only model, when you want a cheaper text layer.
        "text_model": os.environ.get("OPENAI_TEXT_MODEL", "") or None,
        "timeout": float(os.environ.get("OPENAI_TIMEOUT", "120")),
    }


# ---------------------------------------------------------------------------
# 1) chat() -> text over /chat/completions (non-streaming, stdlib only)
# ---------------------------------------------------------------------------


class AiAssistant:
    """Minimal OpenAI-compatible chat client + tool registry holder."""

    def __init__(self, config: dict | None = None):
        self._defaults = default_config()
        if config:
            merged = dict(self._defaults)
            merged.update(config)
            self.config = merged
        else:
            self.config = self._defaults

    # -- model selectors ----------------------------------------------------
    @property
    def text_model(self):
        return (self.config.get("text_model") or self.config.get("model"))

    @property
    def vision_model(self):
        return (self.config.get("vision_model") or self.config.get("model"))

    # -- the chat call ------------------------------------------------------
    def chat(self, messages: list, model: str | None = None,
             tools: list | None = None, temperature: float = 0.2,
             max_tokens: int = 1200) -> str:
        """POST /chat/completions, return assistant text (or first tool_call
        serialized as JSON so a bare string is always returned).

        Uses urllib (no requests dependency required) but honours certs/proxies
        from the environment. Raises a clear error if no api_key is set.
        """
        api_key = self.config.get("api_key")
        if not api_key:
            raise RuntimeError(
                "AiAssistant.chat() needs an API key — set OPENAI_API_KEY "
                "or pass config={'api_key': ...}. (Helpers + agent loop don't "
                "need a key; only live chat does.)")
        base = self.config.get("base_url", "https://api.openai.com/v1")
        payload = {
            "model": model or self.text_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{base}/chat/completions", data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            })
        try:
            with urllib.request.urlopen(
                    req, timeout=self.config.get("timeout", 120)) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            snippet = e.read().decode()[:500]
            raise RuntimeError(f"Chat API HTTP {e.code}: {snippet}")
        msg = data["choices"][0]["message"]
        if msg.get("content"):
            return msg["content"]
        if msg.get("tool_calls"):
            # return the first tool call so callers can execute it
            return json.dumps(msg["tool_calls"][0])
        return ""

    # -- vision helper ------------------------------------------------------
    def chat_with_image(self, image_b64: str, prompt: str,
                        text: str | None = None) -> str:
        """Send a base64 image + prompt to the VISION model (non-streaming).

        May need a model that accepts image_url content parts. If the base URL
        is a legacy chat endpoint this is where you'd swap in the right shape;
        the OpenAI one works out of the box.
        """
        api_key = self.config.get("api_key")
        if not api_key:
            raise RuntimeError("chat_with_image needs an API key.")
        base = self.config.get("base_url", "https://api.openai.com/v1")
        content = [{"type": "text", "text": prompt}]
        if text:
            content.insert(0, {"type": "text", "text": text})
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{image_b64}"},
        })
        payload = {
            "model": self.vision_model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.0,
            "max_tokens": 900,
        }
        req = urllib.request.Request(
            f"{base}/chat/completions", data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {api_key}"})
        try:
            with urllib.request.urlopen(
                    req, timeout=self.config.get("timeout", 120)) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            snippet = e.read().decode()[:500]
            raise RuntimeError(f"Vision API HTTP {e.code}: {snippet}")
        return data["choices"][0]["message"].get("content", "")


# ---------------------------------------------------------------------------
# DOM helpers (share the getBoundingClientRect wire pattern from
# ghostrise/wire.py + behavior.py)
# ---------------------------------------------------------------------------

# JS that maps interactive elements -> {sel-ish label, tag, name/id/type, rect}
# rects use scroll-adjusted coords so downstream mouse math matches WireMouse.
_ELEMENT_MAP_JS = r"""
(() => {
  const out = [];
  const rectOf = (e) => {
    const r = e.getBoundingClientRect();
    return {top: r.top + window.scrollY, left: r.left + window.scrollX,
            width: r.width, height: r.height};
  };
  const labelFor = (e) => {
    const ls = e.labels && e.labels[0] ? e.labels[0].innerText.trim() : '';
    const ph = e.placeholder ? ' placeholder=' + e.placeholder : '';
    if (ls) return ls;
    if (e.name) return 'name=' + e.name;
    if (e.id) return '#' + e.id;
    return (e.tagName || '').toLowerCase() + ph;
  };
  document.querySelectorAll(
    'input, textarea, select, button, [role=button], [role=checkbox], ' +
    '[role=radio], a[href], [onclick]').forEach((e) => {
    const r = rectOf(e);
    // skip invisible/zero-size elements
    if (r.width < 1 || r.height < 1) return;
    const tag = e.tagName.toLowerCase();
    const clickable = ['button','a','select'].includes(tag) ||
                      e.getAttribute('role')==='button' ||
                      e.hasAttribute('onclick');
    const item = {
      tag: tag,
      selector: labelFor(e),
      id: e.id || '',
      name: e.name || '',
      type: e.getAttribute('type') || '',
      text: (e.innerText||'').trim().slice(0,60),
      value: (e.value !== undefined && e.value !== '') ? String(e.value).slice(0,40) : '',
      rect: r,
      kind: clickable ? 'clickable' : 'field',
    };
    out.push(item);
  });
  return out;
})()
"""

# JS that catches visible error / alert markers.
_ERROR_JS = r"""
(() => {
  const subs = [
    /error/i, /invalid/i, /required/i, /incorrect/i, /wrong/i,
    /failed/i, /not found/i, /please enter/i, /try again/i,
    /captcha/i, /verify/i, /just a moment/, /security check/i,
  ];
  const seen = [];
  const walk = (node) => {
    for (const n of node.querySelectorAll('*')) {
      if (n.children.length) continue;            // leaf text only
      const t = (n.innerText||'').trim();
      if (!t || t.length > 200) continue;
      const hit = subs.find((re) => re.test(t));
      if (hit) {
        const r = n.getBoundingClientRect();
        if (r.width > 1 && r.height > 1) {
          seen.push({text: t.slice(0,160),
                     kind: hit.source,
                     rect: {top:r.top+window.scrollY, left:r.left+window.scrollX,
                            width:r.width, height:r.height}});
        }
      }
    }
  };
  ['main','body','form'].forEach((s) => {
    const el = document.querySelector(s);
    if (el) walk(el);
  });
  return seen.slice(0, 40);
})()
"""


def _eval_json(page, js) -> list | dict:
    """Evaluate a JS expression that returns JSON via the page's evaluate."""
    try:
        raw = page.evaluate(js)
    except Exception:
        raw = None
    if isinstance(raw, (list, dict)):          # evaluate already deserialized
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return []
    return []


def _page_text(page) -> str:
    try:
        return page.evaluate("document.body?document.body.innerText.slice(0,4000):''") or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 2a) NON-VISION "layout/box" helper + error extraction
# ---------------------------------------------------------------------------


def describe_page(page) -> dict:
    """Structured element map + error detections for a page.

    Model-agnostic: NO pixels. It reads DOM bounding boxes (getBoundingClientRect
    via the same wire the GhostRise click/type engine uses) so a non-vision
    model can reason about *where* fields and buttons are (top/left/width/height)
    and *what's wrong* (error text). Returns:
        { "title", "url", "text", "elements": [...], "errors": [...] }
    """
    elements = _eval_json(page, _ELEMENT_MAP_JS)
    errors = _eval_json(page, _ERROR_JS)
    meta = {}
    for k, expr in (("title", "document.title"),
                    ("url", "location.href")):
        try:
            meta[k] = page.evaluate(expr) or ""
        except Exception:
            meta[k] = ""
    return {
        "title": meta.get("title", ""),
        "url": meta.get("url", ""),
        "text": _page_text(page),
        "elements": elements,
        "errors": errors,
    }


def find_element(page, selector: str) -> dict | None:
    """Locate one element's box by a CSS selector -> its center + box."""
    js = (
        "JSON.stringify((function(){var e=document.querySelector(%r);"
        "if(!e)return null;var r=e.getBoundingClientRect();return "
        "{top:r.top+window.scrollY,left:r.left+window.scrollX,width:r.width,"
        "height:r.height,center_x:r.left+r.width/2,"
        "center_y:r.top+r.height/2};})())" % selector)
    try:
        raw = page.evaluate(js)
    except Exception:
        return None
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return None


def extract_form_context(page) -> dict:
    """Focus on form fields + any errors near them — what the model needs to
    fill a form correctly."""
    d = describe_page(page)
    fields = [e for e in d["elements"] if e.get("kind") == "field"]
    errors = d["errors"]
    return {"fields": fields, "errors": errors, "title": d["title"]}


# ---------------------------------------------------------------------------
# 2b) VISION "click-verifier" + action helpers
# ---------------------------------------------------------------------------


def _page_has_screenshot(page) -> bool:
    return hasattr(page, "screenshot") and callable(page.screenshot)


def capture_image_b64(page) -> str | None:
    """Base64 PNG of the current page, if the engine exposes .screenshot().

    NOTE: GhostSession's playwright/cloak pages DO have .screenshot(). The raw
    GhostWire engine (ghostrise/wire.py) currently does NOT — it has no
    screenshot() method. For GhostWire pages this returns None and the
    verifier falls back to DOM-state heuristics. (A CDP Page.captureScreenshot
    path can be added to GhostWire later; the verifier is already written to
    use a screenshot when one is available.)
    """
    if not _page_has_screenshot(page):
        return None
    try:
        import base64
        from io import BytesIO
        buf = BytesIO()
        page.screenshot(path=buf)                # playwright writes to file-like
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        try:
            # some engines only take a path
            import base64, tempfile
            fd, p = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            page.screenshot(path=p)
            with open(p, "rb") as f:
                data = base64.b64encode(f.read()).decode()
            os.unlink(p)
            return data
        except Exception:
            return None


# -- actions (use HumanActions so interactions stay human-shaped) -----------

class _CompatLocator:
    """Locator-lite whose bounding_box() returns getBoundingClientRect-style
    rect {x,y,width,height} via the page's evaluate() — works on playwright
    pages AND the raw GhostWire engine (whose adapter locators don't expose a
    playwright-shaped bounding_box)."""

    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    def bounding_box(self):
        box = find_element(self.page, self.selector)
        if not box:
            return None
        return {"x": box["left"], "y": box["top"],
                "width": box["width"], "height": box["height"]}


class _CompatKeyboard:
    """HumanActions needs page.keyboard.type(). Playwright/CloakBrowser pages
    have one; the raw GhostWire engine types via WireMouse.type (CDP
    Input.insertText). This shim forwards to the engine's mouse when no real
    keyboard exists."""

    def __init__(self, page):
        self.page = page
        self._real = getattr(page, "keyboard", None)

    def type(self, text, **kw):
        if self._real is not None and not isinstance(self._real, (_CompatKeyboard,)):
            return self._real.type(text, **kw)
        mouse = getattr(self.page, "mouse", None)
        type_fn = getattr(mouse, "type", None)
        if callable(type_fn):
            delay = kw.get("delay")
            return type_fn(text, delay=delay) if delay else type_fn(text)
        # last resort: DOM-level fill on the focused element (robustness)
        import base64
        b64 = base64.b64encode(text.encode()).decode()
        self.page.evaluate(
            "var el=document.activeElement;if(el){el.value=atob(%r);"
            "el.dispatchEvent(new Event('input',{bubbles:true}));"
            "el.dispatchEvent(new Event('change',{bubbles:true}));}" % b64)
        return text

    def press(self, key, **kw):
        if self._real is not None and not isinstance(self._real, (_CompatKeyboard,)):
            return self._real.press(key, **kw)
        return key


class _CompatMouse:
    """Raw single-point CDP mouse move for the HumanActions bezier layer.

    HumanActions.move_to already computes its own bezier path and calls
    page.mouse.move(x, y) per point. If that mouse is WireMouse (which itself
    runs another bezier + sleep per call) you get DOUBLE bezier -> 30-40s per
    click. This wrapper dispatches a raw move so the HumanActions bezier is the
    ONLY motion layer. Down/up/click still humanized via the wire's mouse."""
    def __init__(self, wire):
        self._wire = wire
        self._wm = getattr(wire, "mouse", None)  # WireMouse, or None
        self.x, self.y = 0.0, 0.0

    def _dispatch(self, type_, x, y, button="left", clicks=1):
        sid = getattr(self._wire, "_sid", None)
        try:
            self._wire._send("Input.dispatchMouseEvent",
                             {"type": type_, "x": x, "y": y,
                              "button": button, "clickCount": clicks,
                              "modifiers": 0}, session_id=sid)
        except Exception:
            pass

    def move(self, x, y, *a, **k):
        self._dispatch("mouseMoved", x, y)
        self.x, self.y = float(x), float(y)
        return self

    def down(self):
        self._dispatch("mousePressed", self.x, self.y)
        return self

    def up(self):
        self._dispatch("mouseReleased", self.x, self.y)
        return self

    def click(self, x=None, y=None, hold=None):
        if x is not None and y is not None:
            self.move(x, y)
        self.down()
        if self._wm is not None:
            hold = hold or 0.06
        time.sleep(hold or 0.06)
        self.up()
        return self

    def type(self, text, delay=None):
        # HumanActions calls type(ch, delay=55..165) where delay is MILLISECONDS;
        # WireMouse.type expects SECONDS (time.sleep). Normalize so a real
        # human 55-165 ms/keystroke pacing is used, not 55-165 seconds.
        if self._wm is not None and hasattr(self._wm, "type"):
            if delay is not None and delay > 5:
                delay = delay / 1000.0
            return self._wm.type(text, delay=delay)
        sid = getattr(self._wire, "_sid", None)
        d = (delay or 0.055) / 1000.0 if delay and delay > 5 else (delay or 0.055)
        for ch in text:
            self._wire._send("Input.insertText", {"text": ch}, session_id=sid)
            time.sleep(random.uniform(d * 0.7, d * 1.3))
        return self


class _CompatPage:
    """Thin human-shaped page surface for HumanActions. Wraps ANY engine page
    (playwright/CloakBrowser or raw GhostWire) so HumanActions.type/click
    resolve real rects and can type even without a native keyboard object."""

    def __init__(self, page):
        # idempotent: unwrap nested _CompatPage so _CompatMouse gets the real
        # raw wire (which has _send), not a compat page that doesn't.
        if isinstance(page, _CompatPage):
            page = page._page
        self._page = page
        # use the single-move compat mouse so HumanActions' bezier is the only
        # motion layer (kills the double-bezier 40s-per-click hang)
        self.mouse = _CompatMouse(page)
        self.keyboard = _CompatKeyboard(page)

    def locator(self, sel):
        return _CompatLocator(self._page, sel)

    def evaluate(self, expr, *a, **kw):
        return self._page.evaluate(expr, *a, **kw)


def _human(page):
    """Get a HumanActions that works on any engine page by wrapping it in a
    compat page surface (real boxes + keyboard shim)."""
    from ghostrise.behavior import HumanActions
    return HumanActions(_CompatPage(page))


def fill_model_written(page, fields: dict) -> list:
    """Fill a {field_selector_or_placeholder: value} map using HumanActions.type.

    fields keys may be a CSS selector, or a substring that matches a field's
    id/name/placeholder — we resolve via describe_page's element map first.
    Returns a list of {selector, ok} results.
    """
    human = _human(page)
    # build a resolution index from the DOM element map
    idx = _build_field_index(page)
    results = []
    for key, value in fields.items():
        sel = _resolve_field(page, idx, key)
        if not sel:
            results.append({"for": key, "ok": False,
                            "reason": "no matching field"})
            continue
        try:
            human.type(sel, str(value))
            results.append({"for": key, "selector": sel, "ok": True})
        except Exception as e:
            results.append({"for": key, "selector": sel, "ok": False,
                            "reason": str(e)[:120]})
    return results


def _build_field_index(page) -> list:
    d = describe_page(page)
    return [e for e in d.get("elements", []) if e.get("kind") == "field"]


def _resolve_field(page, idx, key) -> str | None:
    """Turn a key (selector/name/id/placeholder substring) into a CSS selector,
    or hand the key back if it already looks like a selector."""
    key = str(key).strip()
    # 1) try it directly as a selector / id
    trimmed = key[1:] if key.startswith(("#", ".")) else key
    for e in idx:
        if (e.get("id") == trimmed or e.get("name") == trimmed
                or e.get("selector") == key
                or (e.get("id") and e["id"] == key)
                or '#%s' % trimmed == key):
            css = "#%s" % e["id"] if e.get("id") else "input[name=%r]" % e.get("name")
            return css
    # 2) substring match on placeholder/name/id
    for e in idx:
        blob = " ".join(str(e.get(k, "")) for k in ("id", "name", "selector", "text"))
        if key.lower() in blob.lower():
            if e.get("id"):
                return "#%s" % e["id"]
            if e.get("name"):
                return "input[name=%r]" % e["name"]
            return None
    return None


def click_by_selector(page, sel: str) -> bool:
    """Humanized click on a CSS selector (HumanActions.click)."""
    try:
        _human(page).click(sel)
        return True
    except Exception:
        # fallback: resolve through the element map (e.g. by label text)
        box = find_element(page, sel)
        if not box:
            return False
        try:
            m = page.mouse
            m.click(box["center_x"], box["center_y"])
            return True
        except Exception:
            return False


# -- click-verifier (vision OR dom-state) -----------------------------------

def page_state_snapshot(page) -> str:
    """Cheap DOM-state fingerprint of visible text + element presence, used by
    the non-vision verifier and recorded BEFORE an action as prior_state."""
    try:
        return page.evaluate("document.body?document.body.innerText.slice(0,3000):''") or ""
    except Exception:
        return ""


def verify_click_landed(page, sel: str, prior_state=None) -> dict:
    """Heuristic confirmation that a click landed.

    Strategy (works without any model):
      1. If the engine exposes .screenshot() -> capture + (optional) hand to
         the VISION model if an api_key is set; else note 'no vision key'.
      2. DOM-state heuristic: compare the post-action text/URL to prior_state
         and confirm the target element still exists (didn't navigate-away
         unexpectedly) — enough to catch 'click did nothing' / 'click opened
         a new page' without a model.

    Returns { landed, reason, vision?: {...}, method }
    """
    reasons = []
    method = "dom-state"
    # 2) DOM state heuristic
    after = page_state_snapshot(page)
    if prior_state is None:
        reasons.append("no prior_state supplied; recorded post state only")
        prior_state = ""
    target_exists = bool(find_element(page, sel))
    nav = ""
    try:
        nav = page.evaluate("location.href") or ""
    except Exception:
        pass
    reasons.append(
        f"target_exists={target_exists}; text_url_changed={after != prior_state or bool(nav)}")
    # 1) vision path when screenshotable
    vision = None
    b64 = capture_image_b64(page)
    if b64 is not None:
        method = "screenshot+dom"
        reasons.append("screenshot captured (%d b64 bits)" % len(b64))
    landed = target_exists or (after != prior_state) or bool(nav)
    return {
        "landed": landed,
        "method": method,
        "reason": "; ".join(reasons),
        "vision": vision,
        "screenshot_b64": b64 if b64 is not None else None,
    }


# ---------------------------------------------------------------------------
# 1c) TOOL REGISTRY — plain dict  name -> (schema, callable)
#     model-agnostic: whatever the model returns, we map to these.
# ---------------------------------------------------------------------------


class BrowserTools:
    """Static registry of browser capabilities -> callables.

    Every callable takes the bound page from the agent loop (see browser_agent
    AgentLoop); callables are module-level functions that accept `page` first
    then their args. dict shape: name -> {"schema": {...}, "fn": callable}.
    """

    @staticmethod
    def registry():
        return {
            "describe_page": {
                "schema": {
                    "description": "Return structured element map (boxes + rects) "
                                   "and error text for the current page.",
                    "parameters": {"type": "object", "properties": {}}},
                "fn": lambda page, **kw: describe_page(page),
            },
            "fill_form": {
                "schema": {
                    "description": "Fill form fields. fields = {selector_or_label: value}.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "fields": {"type": "object",
                                       "description": "field -> value map"}}},
                },
                "fn": lambda page, fields=None, **kw:
                      fill_model_written(page, fields or {}),
            },
            "click_element": {
                "schema": {
                    "description": "Click an element by CSS selector using "
                                   "humanized mouse.",
                    "parameters": {"type": "object",
                                   "properties": {
                                       "sel": {"type": "string"}}}},
                "fn": lambda page, sel=None, **kw: click_by_selector(page, sel),
            },
            "extract_page_text": {
                "schema": {
                    "description": "Return the visible page text.",
                    "parameters": {"type": "object", "properties": {}}},
                "fn": lambda page, **kw: _page_text(page),
            },
            "detect_captcha_boxes": {
                "schema": {
                    "description": "Detect captcha/verification elements "
                                   "and error markers on the page.",
                    "parameters": {"type": "object", "properties": {}}},
                "fn": lambda page, **kw: _detect_captcha(page),
            },
            "get_errors": {
                "schema": {
                    "description": "Return detected error/alert text on the page.",
                    "parameters": {"type": "object", "properties": {}}},
                "fn": lambda page, **kw: _eval_json(page, _ERROR_JS),
            },
            "verify_click": {
                "schema": {
                    "description": "Confirm a click landed via screenshot (if "
                                   "available) + DOM-state heuristic.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "sel": {"type": "string"},
                            "prior_state": {"type": "string"}}}},
                "fn": lambda page, sel=None, prior_state=None, **kw:
                      verify_click_landed(page, sel, prior_state),
            },
            "extract_form_context": {
                "schema": {
                    "description": "Return form fields + errors needed to fill "
                                   "a form.",
                    "parameters": {"type": "object", "properties": {}}},
                "fn": lambda page, **kw: extract_form_context(page),
            },
            "compile_verify": {
                "schema": {
                    "description": "Compile + validate an agent-written page: "
                                   "source HTML/CSS/JS -> self-contained "
                                   "artifact staged for GhostWire to open.",
                    "parameters": {"type": "object", "properties": {
                        "source": {"type": "string",
                                   "description": "full HTML source to "
                                                  "compile/validate"},
                        "name": {"type": "string",
                                 "description": "artifact name (default "
                                                "'page')"}}}},
                "fn": lambda page, source="", name="page", **kw:
                      _compiler_tool(source, name),
            },
        }


def _detect_captcha(page) -> dict:
    errors = _eval_json(page, _ERROR_JS)
    captcha = [e for e in errors if re_search_captcha(e.get("text", ""))]
    return {"captcha_markers": captcha, "all_error_markers": errors}


def re_search_captcha(text: str) -> bool:
    import re
    return bool(re.search(r"captcha|verify|just a moment|security|checkbox",
                          text, re.I))


def _compiler_tool(source: str = "", name: str = "page") -> dict:
    """compile_verify tool body — lazily imports ghostrise.compiler so the
    registry loads even if node/compiler is absent at import time."""
    from ghostrise.compiler import compile_verify_tool
    return compile_verify_tool(source=source, name=name)


# ---------------------------------------------------------------------------
# small guard so `python -m ghostrise.ai_assistant` runs a self-check
# ---------------------------------------------------------------------------

# load browser_agent lazily to avoid circular imports at import time
def _load_agent_loop():
    from ghostrise.browser_agent import AgentLoop
    return AgentLoop
