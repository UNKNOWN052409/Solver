"""GhostRise Browser Compiler — write -> validate -> package -> stage.

The compiler bridges what an agent *writes* (HTML/CSS/JS source) and what
GhostWire *opens* (a self-contained single-page artifact on disk). It is the
"compile + verify" stage of the browser pipeline that BROWSER_FEATURES_AUDIT
lists as a gap.

Pipeline (`BrowserCompiler.compile`):

  1. VALIDATE HTML    — stdlib `html.parser.HTMLParser` (well-formedness of
                        tags + a light structural check: no unclosed <script>
                        that eats the rest of the page).
  2. VALIDATE JS      — REAL engine when available: `node --check file.js` is a
                        real parse of the JS (node ships a full JS parser).
                        If `node` is missing we fall back to a strict
                        bracket/quote/comment sanity scan (never claims more
                        than it checks).
  3. INLINE ASSETS    — external CSS/JS `<link>` / `<script src>` that point at
                        local relative files get inlined (self-contained page).
  4. STAGE            — write the artifact (plus a small manifest.json) into a
                        runtime dir GhostWire can open with `w.goto("file://…")`.

Exit contract (the shape the AgentLoop `compile_verify` tool returns):
    {
      "ok": bool,
      "artifact": "path/to/artifact.html",
      "js_engine": "node" | "fallback",
      "js_errors": [...],          # empty when clean
      "html_errors": [...],        # empty when clean
      "assets_inlined": int,
      "size_bytes": int,
      "manifest": "path",
    }

No network, no LLM, no external deps beyond the optional `node` binary —
pure stdlib + subprocess. Safe to import on any box (node missing just
degrades JS validation to the fallback scanner).
"""

from __future__ import annotations

import html.parser
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile

log = logging.getLogger("ghostrise.compiler")

DEFAULT_RUNTIME_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "runtime"
)


# --------------------------------------------------------------------------- #
# HTML validation (stdlib html.parser)
# --------------------------------------------------------------------------- #
class _HtmlChecker(html.parser.HTMLParser):
    """Well-formedness/violation collector for agent-written HTML.

    We are deliberately *lenient* — agents write real-world HTML that is never
    perfectly spec-valid, so we only hard-fail on problems that would actually
    break the page in a browser: unbalanced <script>/<style>/<html>/<body> and
    parser panics (malformed start/end tags). Everything else (unclosed <p>,
    odd entities) is collected as a warning, not an error.
    """

    # tags that must nest cleanly — an unbalanced one kills page rendering
    _STRUCT_TAGS = ("html", "head", "body", "script", "style", "iframe")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.errors = []
        self.warnings = []
        self._stack = []

    def handle_starttag(self, tag, attrs):
        if tag in self._STRUCT_TAGS:
            self._stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        pass  # self-closing — no push

    def handle_endtag(self, tag):
        if tag not in self._STRUCT_TAGS:
            return
        if self._stack and self._stack[-1] == tag:
            self._stack.pop()
        elif tag in self._stack:
            # mismatched close — pop down to it (browser repairs silently)
            self.warnings.append(f"mismatched close </{tag}> (stack {self._stack})")
            while self._stack and self._stack[-1] != tag:
                self._stack.pop()
            if self._stack:
                self._stack.pop()
        else:
            self.warnings.append(f"stray close </{tag}>")

    def handle_data(self, data):
        pass

    def error(self, message):
        # html.parser calls error() on malformed markup (illegal chars etc.)
        self.errors.append(f"malformed markup: {message}")


def validate_html(source: str) -> dict:
    """Parse-check HTML source. Returns {errors, warnings}."""
    checker = _HtmlChecker()
    try:
        checker.feed(source)
        checker.close()
    except Exception as e:  # noqa: BLE001 — collect, don't raise
        checker.errors.append(f"html parse raised: {e}")
    # unbalanced structural tags that remain open = real breakage
    leftover = checker._stack
    net_errors = list(checker.errors)
    if leftover:
        net_errors.append(f"unclosed structural tag(s): {leftover}")
    return {"errors": net_errors, "warnings": checker.warnings}


# --------------------------------------------------------------------------- #
# JS validation: real node --check, else strict bracket scanner
# --------------------------------------------------------------------------- #

def _find_node() -> str | None:
    return shutil.which("node") or shutil.which("nodejs")


def validate_js_node(source: str) -> list:
    """REAL JS parse via `node --check`. Returns list of error strings."""
    node = _find_node()
    if not node:
        return [_JS_UNSET]
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(source)
        tmp = fh.name
    try:
        proc = subprocess.run([node, "--check", tmp], capture_output=True,
                              text=True, timeout=20)
    except Exception as e:  # noqa: BLE001
        return [f"node --check crashed: {e}"]
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if proc.returncode == 0:
        return []
    return [line for line in (proc.stderr or "").splitlines() if line.strip()]


# sentinel: node missing -> caller uses the fallback scanner
_JS_UNSET = "__NODE_MISSING__"


# bracket/comment scanner — ONLY used when node is unavailable. It checks
# balance of (), {}, [], string literals and skips comments, a genuine (if
# weaker) sanity pass, never a full parse.
_JS_PAIR = {"(": ")", "{": "}", "[": "]"}


def validate_js_fallback(source: str) -> list:
    """Strict bracket/quote/comment sanity scan (no JS engine)."""
    errors = []
    stack = []
    i, n = 0, len(source)
    line = 1
    while i < n:
        c = source[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c == "/" and i + 1 < n:
            nxt = source[i + 1]
            if nxt == "/":  # line comment
                j = source.find("\n", i)
                i = n if j == -1 else j + 1
                continue
            if nxt == "*":  # block comment
                end = source.find("*/", i + 2)
                if end == -1:
                    errors.append(f"line {line}: unterminated block comment /*")
                    break
                line += source[i:end].count("\n")
                i = end + 2
                continue
        if c in "\"'`":  # string literal — skip to matching close (simple)
            q = c
            j = i + 1
            while j < n:
                if source[j] == "\\":
                    j += 2
                    continue
                if source[j] == q:
                    break
                if source[j] == "\n" and q != "`":
                    errors.append(f"line {line}: unterminated string")
                    break
                j += 1
            line += source[i:j + 1].count("\n")
            i = j + 1
            continue
        if c in "({[":
            stack.append((c, line))
        elif c in ")}]":
            if not stack:
                errors.append(f"line {line}: unmatched closing '{c}'")
            else:
                opener, _ = stack.pop()
                if _JS_PAIR[opener] != c:
                    errors.append(f"line {line}: '{opener}' closed by '{c}'")
        i += 1
    for opener, ln in stack:
        errors.append(f"line {ln}: unclosed '{opener}'")
    return errors


def validate_js(source: str) -> dict:
    """Validate JS — real node parse when available, else fallback scanner.

    Returns {"engine": "node"|"fallback", "errors": [...], "missing_node": bool}
    """
    node = _find_node()
    if node:
        errors = validate_js_node(source)
        if errors == [_JS_UNSET]:  # engine present but crashed -> fallback
            return {"engine": "node", "errors": validate_js_fallback(source),
                    "missing_node": False}
        return {"engine": "node", "errors": errors, "missing_node": False}
    return {"engine": "fallback", "errors": validate_js_fallback(source),
            "missing_node": True}


# --------------------------------------------------------------------------- #
# Asset inlining + packaging
# --------------------------------------------------------------------------- #
_RE_CSS_LINK = re.compile(r"<link[^>]*rel=[\"']?stylesheet[\"']?[^>]*>",
                          re.IGNORECASE)
_RE_SCRIPT_SRC = re.compile(r"<script([^>]*)\bsrc=[\"']([^\"']+)[\"']([^>]*)>"
                            r"(.*?)</script>", re.IGNORECASE | re.DOTALL)
_RE_SCRIPT_TAG = re.compile(r"<script[^>]*>.*?</script>",
                            re.IGNORECASE | re.DOTALL)


def _extract_inline_js(source: str) -> list:
    """All inline (non-src) <script> bodies — for JS validation."""
    block_bodies = []
    for m in _RE_SCRIPT_TAG.finditer(source):
        tag = m.group(0)
        if re.search(r"\bsrc\s*=", tag, re.IGNORECASE):
            continue  # external — validated via node --check file too, but
        body = re.sub(r"^<script[^>]*>", "", tag, flags=re.IGNORECASE)
        body = re.sub(r"</script>$", "", body, flags=re.IGNORECASE)
        block_bodies.append((m.start(), body))
    return block_bodies


def inline_assets(source: str, base_dir: str) -> tuple[str, int]:
    """Inline local stylesheet/script srcs into one self-contained page.

    Only *relative local* files (existing under base_dir) are inlined;
    absolute http(s)/data: URLs are left untouched. Returns (new_source, count).
    """
    count = 0

    def _load(path):
        nonlocal count
        cand = os.path.join(base_dir, path)
        if not path.startswith(("http://", "https://", "data:", "//")) \
                and os.path.isfile(cand):
            try:
                with open(cand, "r", encoding="utf-8", errors="replace") as fh:
                    count += 1
                    return fh.read()
            except OSError:
                return None
        return None

    # CSS links -> <style>
    def _css_repl(m):
        href = re.search(r"href=[\"']([^\"']+)[\"']", m.group(0), re.IGNORECASE)
        if not href:
            return m.group(0)
        css = _load(href.group(1))
        if css is None:
            return m.group(0)
        return f"<style>\n{css}\n</style>"

    source = _RE_CSS_LINK.sub(_css_repl, source)

    # script src -> inline body (keep attrs other than src)
    def _js_repl(m):
        attrs = (m.group(1) or "").strip()
        attrs_tail = (m.group(3) or "").strip()
        src = m.group(2)
        js = _load(src)
        if js is None:
            return m.group(0)
        keep = f" {attrs} {attrs_tail}".strip()
        return f"<script{keep}>\n{js}\n</script>"

    source = _RE_SCRIPT_SRC.sub(_js_repl, source)
    return source, count


# --------------------------------------------------------------------------- #
# The compiler
# --------------------------------------------------------------------------- #
class BrowserCompiler:
    """compile(source, ...) -> packaged self-contained artifact + validation.

    Usage:
        bc = BrowserCompiler()
        res = bc.compile("<html>….js…</html>", name="hello")
        # res ok=True -> open with GhostWire:
        #   w.goto("file://" + res["artifact"])
    """

    def __init__(self, runtime_dir: str = DEFAULT_RUNTIME_DIR):
        self.runtime_dir = runtime_dir
        os.makedirs(self.runtime_dir, exist_ok=True)

    # -- helpers ------------------------------------------------------------
    def _script_blocks(self, source: str):
        return _extract_inline_js(source)

    def validate(self, source: str, base_dir: str | None = None) -> dict:
        """Validate html + js -> {html_errors, js_engine, js_errors, warnings}.

        `base_dir` lets external <script src>/<link> files also be js-checked
        (their inline content, read from disk) — mirror of the real page.
        """
        html = validate_html(source)
        js_engine = "fallback"
        js_errors = []
        # inline script blocks
        blocks = self._script_blocks(source)
        if blocks:
            joined = "\n;\n".join(body for _, body in blocks)
            jv = validate_js(joined)
            js_engine = jv["engine"]
            js_errors = list(jv["errors"])
        # external script files (if base_dir given)
        if base_dir:
            ext_errors = []
            for m in _RE_SCRIPT_SRC.finditer(source):
                src = m.group(2)
                if src.startswith(("http://", "https://", "data:", "//")):
                    continue
                path = os.path.join(base_dir, src)
                if os.path.isfile(path):
                    try:
                        with open(path, "r", encoding="utf-8",
                                  errors="replace") as fh:
                            ext_errors += validate_js(fh.read())["errors"]
                    except OSError:
                        pass
            js_errors += ext_errors
        return {"html_errors": html["errors"], "warnings": html["warnings"],
                "js_engine": js_engine, "js_errors": js_errors}

    # -- main ---------------------------------------------------------------
    def compile(self, source: str, name: str = "page",
                base_dir: str | None = None) -> dict:
        """Validate + package + stage. Returns the exit-contract dict.

        Raises ValueError on validation failure (the artifact is not written).
        """
        results = self.validate(source, base_dir=base_dir)
        ok = not results["html_errors"] and not results["js_errors"]
        if not ok:
            return {
                "ok": False,
                "artifact": None,
                "js_engine": results["js_engine"],
                "js_errors": results["js_errors"],
                "html_errors": results["html_errors"],
                "assets_inlined": 0,
                "size_bytes": len(source.encode("utf-8")),
                "manifest": None,
            }
        # inline local assets -> self-contained page
        artifact_src, inlined = inline_assets(source, base_dir or ".")
        artifact_path = os.path.join(self.runtime_dir, f"{name}.html")
        with open(artifact_path, "w", encoding="utf-8") as fh:
            fh.write(artifact_src)
        manifest = {
            "name": name, "ok": True,
            "js_engine": results["js_engine"],
            "assets_inlined": inlined,
            "size_bytes": len(artifact_src.encode("utf-8")),
            "artifact": os.path.basename(artifact_path),
        }
        manifest_path = os.path.join(self.runtime_dir, f"{name}.manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        return {
            "ok": True,
            "artifact": artifact_path,
            "js_engine": results["js_engine"],
            "js_errors": [],
            "html_errors": [],
            "assets_inlined": inlined,
            "size_bytes": manifest["size_bytes"],
            "manifest": manifest_path,
        }

    # -- AgentLoop-compiler-verify tool surface ----------------------------
    def compile_verify(self, source: str, name: str = "page") -> dict:
        """Tool-shaped wrapper: `compile()` but returns the dict even on
        failure (AgentLoop surfaces validation errors to the caller without
        raising)."""
        try:
            return self.compile(source, name=name)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "artifact": None, "js_engine": "err",
                    "js_errors": [str(e)], "html_errors": [],
                    "assets_inlined": 0, "size_bytes": 0, "manifest": None}


# default singleton — browser_agent / AgentLoop registry binds to this
_compiler = BrowserCompiler()


def compile_verify_tool(page=None, source: str = "", name: str = "page",
                        **kwargs) -> dict:
    """Standalone entry the AgentLoop registry calls (page is ignored for
    compile — it compiles source text, not the current page)."""
    if not source:
        return {"ok": False, "artifact": None, "js_engine": "n/a",
                "js_errors": ["no source given"], "html_errors": [],
                "assets_inlined": 0, "size_bytes": 0, "manifest": None}
    return _compiler.compile_verify(source, name=name)


# --------------------------------------------------------------------------- #
# CLI: `python -m ghostrise.compiler file.html [name]`
# --------------------------------------------------------------------------- #
def main(argv=None):
    import sys
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: python -m ghostrise.compiler <file.html> [name]",
              file=sys.stderr)
        return 2
    path = argv[0]
    name = argv[1] if len(argv) > 1 else os.path.splitext(os.path.basename(path))[0]
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        source = fh.read()
    res = _compiler.compile(source, name=name, base_dir=os.path.dirname(path))
    print(json.dumps(res, indent=2))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
