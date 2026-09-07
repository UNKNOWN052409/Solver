#!/usr/bin/env python3
"""qwen-bridge — Qwen Studio ka UNIFIED multi-endpoint aggregator.

Ek hi local HTTP server jo Qwen ke saare gen-features expose karta hai,
fresh token (OAuth-captured) se, history auto-clear ke saath.

Endpoints (OpenAI-style, one port):
  POST /v1/chat/completions   -> text (t2t)  [model=qwen3.8-max]
  POST /v1/image/generations  -> image gen  (chat_type=image)
  POST /v1/video/generations  -> video gen  (chat_type=video / wan)
  POST /v1/code               -> coding + tool-call
  GET  /auth/oauth            -> OAuth capture LINK (browser login -> token)
  GET  /health

History-auto-clear: har request ke baad server-side chat-conversation
delete attempt (best-effort) — chat "history na dikhe".

Anti-captcha: Alibaba WAF (aliyun_waf_aa/RGV587) direct-fetch block.
Isliye saare API calls BROWSER-context fetch se (GhostWire page-eval,
credentials:include + token) — direct urllib WAF HTML deta hai.
"""
import json, os, time, sys, uuid, re, threading
sys.path.insert(0, "/home/kali/NeoSolver")
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------- config ----------
BASE = "https://chat.qwen.ai"
PORT = int(os.environ.get("QWEN_BRIDGE_PORT", "8050"))
TOKEN_FILE = "/home/kali/Rev/qwen_token.json"

# ---------- token store (OAuth-captured) ----------
def load_token():
    try:
        with open(TOKEN_FILE) as f:
            return json.load(f).get("token", "")
    except Exception:
        return ""

def save_token(tok):
    with open(TOKEN_FILE, "w") as f:
        json.dump({"token": tok, "umid": "", "captured": int(time.time())}, f)

# ---------- GhostWire browser-context fetch (WAF-safe) ----------
_wire = None
_wire_lock = threading.Lock()

def _get_wire():
    global _wire
    with _wire_lock:
        if _wire is None:
            import urllib.request, websockets.sync.client as wsc
            from ghostrise.wire import GhostWire
            tok = load_token()
            w = None
            # try existing manual chromium 9227 first (fast path)
            try:
                ws = json.loads(urllib.request.urlopen(
                    "http://127.0.0.1:9227/json/version", timeout=4).read())
                w = GhostWire.__new__(GhostWire)
                w._ws = wsc.connect(ws["webSocketDebuggerUrl"], max_size=None)
                w._msg_id = 0
                tg = w._send("Target.getTargets")
                pages = [t for t in tg.get("targetInfos", []) if t.get("type") == "page"]
                if pages:
                    w._sid = w._send("Target.attachToTarget", {"targetId": pages[0]["targetId"], "flatten": True})["sessionId"]
            except Exception:
                w = None
            # launch own if needed
            if w is None:
                w = GhostWire(headless=True)
                w.launch()
                w.goto(BASE)
            # ensure token + navigate to qwen (so fetch same-origin + WAF-clear)
            try:
                w._send("Page.addScriptToEvaluateOnNewDocument", {
                    "source": f"localStorage.setItem('token', {json.dumps(tok)});"
                }, session_id=w._sid)
            except Exception:
                pass
            try:
                w._send("Page.navigate", {"url": BASE}, session_id=w._sid)
            except Exception:
                w.evaluate(f"localStorage.setItem('token', {json.dumps(tok)})")
            time.sleep(8)
            _wire = w
        return _wire

def page_fetch(method, path, body=None, token=None, timeout_s=90):
    """Browser-context fetch — WAF-safe. FIRE-AND-POLL pattern:
    fetch background me kick (awaitPromise nahi -> WS recv short),
    window.__qwen_res sentinel set karo, phir short evaluate polls me
    result uthao. Bina is pattern ke chat-completions 30s+ recv timeout
    maarti hai (LLM slow)."""
    w = _get_wire()
    tok = token or load_token()
    b = json.dumps(body or {})
    # 1) kick-off (non-blocking): set sentinel, fire inner async WITHOUT
    #    awaiting it. w.evaluate me awaitPromise=true hai — isliye outer
    #    expression immediately return karta hai (inner promise detached).
    kick = f"""(() => {{
      window.__qwen_res = {{done: false}};
      localStorage.setItem('token', {json.dumps(tok)});
      (async () => {{
        try {{
          const ctrl = new AbortController();
          window.__qwen_ctrl = ctrl;
          const t = setTimeout(() => ctrl.abort(), {int(timeout_s*1000)});
          const r = await fetch({json.dumps(path)}, {{
            method: {json.dumps(method)},
            headers: {{'Content-Type': 'application/json', 'Authorization': 'Bearer ' + localStorage.getItem('token')}},
            credentials: 'include',
            body: {json.dumps(b)},
          }});
          window.__qwen_res = {{done: true, status: r.status, text: ''}};
          const txt = await r.text();
          window.__qwen_res.text = txt.slice(0, 12000);
        }} catch(e) {{
          window.__qwen_res = {{done: true, status: 0, text: 'ERR::' + e}};
        }}
      }})();
      return 'kicked';
    }})()"""
    try:
        # use proven w.evaluate() path (Runtime.enable via _send works);
        # if Runtime domain not registered, enable it first
        w._send("Runtime.enable", session_id=w._sid)
        w.evaluate("window.__qwen_res = {done:false}")
        # kick via w.evaluate (RPC id-match — proven in direct tests)
        w.evaluate(kick)
    except Exception as e:
        return 500, json.dumps({"error": "kick " + str(e)[:100]})
    # 2) poll sentinel (short evaluates — no 30s recv hang)
    import time as _t
    deadline = _t.time() + timeout_s
    while _t.time() < deadline:
        _t.sleep(2)
        try:
            r = w.evaluate("JSON.stringify(window.__qwen_res)")
            if r and isinstance(r, str):
                st = json.loads(r)
                if st.get("done"):
                    return st.get("status", 0), st.get("text", "")
        except Exception:
            pass
    return 504, json.dumps({"error": "poll timeout"})

def chat_gen(body, want_chat_type=None):
    """qwen chat completions — text/image/video unified."""
    model = body.get("model", "qwen3.8-max")
    msg = body.get("message") or body.get("messages") or ""
    if isinstance(msg, list):
        msg = " ".join(m.get("content", "") for m in msg)
    chat_type = body.get("chat_type") or want_chat_type or "t2t"
    # history-clear: har request se pehle delete-conversation (best-effort)
    clear_history()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": msg}],
        "stream": False,
        "chat_type": chat_type,
    }
    return page_fetch("POST", "/api/v2/chat/completions", payload)

def clear_history():
    """Chat history auto-clear — server-side conversation delete attempt."""
    try:
        st, body = page_fetch("GET", "/api/v2/chats/?page=1&exclude_project=true")
        if st == 200:
            try:
                chats = json.loads(body).get("data", [])
                for c in chats:
                    cid = c.get("id")
                    if cid:
                        try:
                            page_fetch("DELETE", f"/api/v2/chats/{cid}")
                        except Exception:
                            pass
            except Exception:
                pass
    except Exception:
        pass

def oauth_capture():
    """OAuth link — browser login karke token harvest (existing login_auto flow)."""
    try:
        import subprocess
        # login_auto.py already has the google-oauth+harvest flow
        r = subprocess.run(
            [sys.executable, "/home/kali/Rev/login_auto.py"],
            capture_output=True, text=True, timeout=150,
            env={**os.environ, "DISPLAY": ":99", "MOZ_DISABLE_CONTENT_SANDBOX": "1"},
        )
        return True, r.stdout[-800:]
    except Exception as e:
        return False, str(e)[:200]

# ---------- HTTP handler ----------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def _json(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())
    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True, "token": bool(load_token())})
        elif self.path == "/auth/oauth":
            # generate OAuth capture link (browser) — return as redirect doc
            ok, out = oauth_capture()
            self._json(200 if ok else 500, {"oauth_captured": ok, "detail": out[:400]})
        elif self.path == "/v1/models":
            # qwen ke real models (API v2) — bridge se expose
            st, txt = page_fetch("GET", "/api/v2/models/")
            try:
                raw = json.loads(txt) if txt.startswith("{") else {"data": []}
                self._json(st, raw)
            except Exception:
                self._json(st, {"data": []})
        elif self.path.startswith("/token"):
            self._json(200, {"token": load_token()[:20] + "...", "len": len(load_token())})
        else:
            self._json(404, {"error": "unknown"})
    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        if self.path == "/v1/chat/completions":
            st, txt = chat_gen(body, "t2t")
            self._json(st, json.loads(txt) if txt.startswith("{") else {"raw": txt})
        elif self.path == "/v1/image/generations":
            st, txt = chat_gen(body, "image")
            self._json(st, json.loads(txt) if txt.startswith("{") else {"raw": txt})
        elif self.path == "/v1/video/generations":
            st, txt = chat_gen(body, "video")
            self._json(st, json.loads(txt) if txt.startswith("{") else {"raw": txt})
        elif self.path == "/v1/code":
            # coding + tool-call: inject code-gen marker
            body["chat_type"] = "t2t"
            body["model"] = body.get("model", "qwen3.8-max")
            st, txt = chat_gen(body, "t2t")
            self._json(st, json.loads(txt) if txt.startswith("{") else {"raw": txt})
        else:
            self._json(404, {"error": "unknown"})

def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"qwen-bridge on http://127.0.0.1:{PORT}")
    print("  POST /v1/chat/completions | /v1/image/generations | /v1/video/generations | /v1/code")
    print("  GET  /auth/oauth (OAuth capture) | /health | /token")
    print("  history auto-clear ON")
    srv.serve_forever()

if __name__ == "__main__":
    main()
