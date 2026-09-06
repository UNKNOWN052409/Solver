"""GhostRise search-engine harness — model mountable search tools.

A plain, model-agnostic registry of callables + their JSON schemas, so any
mounted model (agent in ai_assistant.py, an external MCP client, curl) can
call the browser/Solver as a search engine.

Tools
-----
  web_search(query)   -> top results (title, url, snippet) for a query
  web_extract(url)    -> readable text of a page
  browse(query)       -> drive the real browser (GhostWire) at a URL/query

Two ways to call:

  1. In-process registry (for a mounted model in the same Python):
         tools = build_mcp_search_tools()
         tools["web_search"][1]("ghost engine")
             # (name, schema, callable)  OR  (schema, callable)
         -- see build_mcp_search_tools for the exact tuple shape.

  2. HTTP JSON endpoint (for an external client / model):
         python3 -m ghostrise.search_mcp --port 8899
         curl -s -X POST localhost:8899/rpc \\
              -d '{"tool":"web_search","args":{"query":"hello"}}'

Both routes return dicts that serialize to JSON.
"""

from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ------------------------------------------------------------ scrape utils ---


def _fetch(url: str, timeout: float = 20.0) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/131.0.0.0 Safari/537.36")},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _strip_tags(raw: str, limit: int = 4000) -> str:
    text = re.sub(r"<script.*?</script>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


# --------------------------------------------------------------- web_search ---


def _web_search(query: str, limit: int = 8) -> dict:
    """Search DuckDuckGo's HTML endpoint for top results."""
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    raw = _fetch(url)
    # each result: <a class="result__a">title</a> ... <a class="result__snippet">
    titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', raw, flags=re.S)
    snip = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', raw, flags=re.S)
    hrefs = re.findall(r'class="result__a"[^>]*href="([^"]+)"', raw)
    results = []
    for i in range(min(limit, len(titles))):
        title = _strip_tags(titles[i]) if i < len(titles) else ""
        snippet = _strip_tags(snip[i], 300) if i < len(snip) else ""
        href = hrefs[i] if i < len(hrefs) else ""
        if href.startswith("//duckduckgo.com/l/?uddg="):
            href = urllib.parse.unquote(
                href.split("uddg=", 1)[1].split("&", 1)[0])
        results.append({
            "title": title,
            "url": href,
            "snippet": snippet,
        })
    return {"query": query, "results": results}


# --------------------------------------------------------------- web_extract ---


def _web_extract(url: str, limit: int = 4000) -> dict:
    raw = _fetch(url)
    return {"url": url, "title": _strip_tags(re.search(
        r"<title[^>]*>(.*?)</title>", raw, flags=re.S | re.I).group(1), 200)
        if re.search(r"<title[^>]*>(.*?)</title>", raw, flags=re.S | re.I) else "",
        "text": _strip_tags(raw, limit)}


# ------------------------------------------------------------------- browse ---


def _browse(url_or_query: str) -> dict:
    """Open a URL (or a web search for a query) in the real browser engine.

    Uses GhostWire (ghostrise.wire) — raw-CDP chromium.  Falls back cleanly
    if no chromium binary is installed on this box.
    """
    from ghostrise.runtime import resolve

    url = url_or_query
    if "://" not in url:
        url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(url)
    prof = resolve()
    try:
        from ghostrise.wire import GhostWire
    except Exception as e:  # noqa: BLE001
        return {"url": url, "ok": False, "error": f"wire unavailable: {e}"}
    try:
        with GhostWire(headless=True, extra_args=prof["browser_args"]) as w:
            w.goto(url)
            return {"url": url, "ok": True, "title": _b_title(w),
                    "text": w.text(limit=3000)}
    except Exception as e:  # noqa: BLE001 (no chromium here -> clean msg)
        return {"url": url, "ok": False, "error": str(e)}


def _b_title(w) -> str:
    try:
        return w.evaluate("document.title") or ""
    except Exception:
        return ""


# ---------------------------------------------------------------- registry ---

SCHEMAS = {
    "web_search": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "search query"},
            "limit": {"type": "integer", "default": 8},
        },
        "required": ["query"],
    },
    "web_extract": {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "limit": {"type": "integer", "default": 4000},
        },
        "required": ["url"],
    },
    "browse": {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "URL to open, or a search query"},
        },
        "required": ["query"],
    },
}


def build_mcp_search_tools() -> dict:
    """Registry of name -> (schema, callable).

    Shape: {name: (json-SCHEMA-dict, callable(**args) -> dict)}.
    Mounted models in ai_assistant.py import this and call the callable.
    """
    return {
        "web_search": (SCHEMAS["web_search"], _dispatch_search),
        "web_extract": (SCHEMAS["web_extract"], _dispatch_extract),
        "browse": (SCHEMAS["browse"], _dispatch_browse),
    }


# thin named dispatchers so the registry stays introspectable
def _dispatch_search(query: str, limit: int = 8) -> dict:
    return _web_search(query, limit)


def _dispatch_extract(url: str, limit: int = 4000) -> dict:
    return _web_extract(url, limit)


def _dispatch_browse(query: str) -> dict:
    return _browse(query)


# ------------------------------------------------------------------- HTTP ---


def _rpc(body: dict) -> dict:
    tool = body.get("tool")
    args = body.get("args") or {}
    reg = build_mcp_search_tools()
    if tool not in reg:
        return {"ok": False, "error": f"unknown tool: {tool}",
                "tools": sorted(reg.keys())}
    schema, callable_ = reg[tool]
    try:
        result = callable_(**args)
        return {"ok": True, "tool": tool, "result": result}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "tool": tool, "error": str(e)}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def do_POST(self):  # noqa: N802
        try:
            ln = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(ln) if ln else b"{}"
            body = json.loads(raw.decode("utf-8", "ignore"))
        except Exception as e:  # noqa: BLE001
            body = {"error": f"bad request: {e}"}
        out = json.dumps(_rpc(body)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):  # noqa: N802  — tool list / health
        out = json.dumps({"tools": sorted(SCHEMAS.keys()),
                          "endpoint": "POST /rpc  {tool, args}"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def run_server(host: str = "127.0.0.1", port: int = 8899) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), _Handler)
    print(f"search_mcp http://{host}:{port}  (POST /rpc {{tool,args}})")
    srv.serve_forever()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(prog="search_mcp")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--check-tools", action="store_true",
                    help="print registry and exit")
    a = ap.parse_args()
    if a.check_tools:
        for name, (schema, fn) in build_mcp_search_tools().items():
            print(f"{name:12s} schema={json.dumps(schema)}")
        raise SystemExit(0)
    run_server(port=a.port)
