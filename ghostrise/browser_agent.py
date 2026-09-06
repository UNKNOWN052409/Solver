"""GhostRise light agent loop — turn a natural-language instruction into
real browser actions, without any external agent framework.

Pieces:
  * `TOOL_MATCHES` — keyword maps a plain-English instruction ("form fill kar
    de", "mark kar de", "click", "read text", "check errors") onto a tool in
    the registry. Pure Python, no LLM required for the matching step.
  * `AgentLoop`    — wires the instruction -> tool -> execute -> result path.
    It can run with a REAL model via AiAssistant.chat() (which asks the model
    for "which tool + args?" JSON), OR with a mock model for offline testing
    (see `mock_model` below and the D2 test in /tmp).

The registry itself lives in ai_assistant.BrowserTools.registry(); this module
only concerns itself with *deciding which tool to call and running it*.
"""

import json
import re

from ghostrise.ai_assistant import (
    BrowserTools, default_config, AiAssistant,
    click_by_selector, fill_model_written, describe_page,
    extract_form_context, _page_text, verify_click_landed, find_element,
)


# ---------------------------------------------------------------------------
# instruction -> tool matching (offline, model-agnostic)
# ---------------------------------------------------------------------------

# (regex, tool_name) — first match wins. Order matters: specific first.
TOOL_MATCHES = [
    # fill a form
    (r"fill|type|enter.+form|form.+fill|bhar|bharna|likh|daal|dal de",
     "fill_form"),
    # mark / check a task, click / press a button
    (r"mark|mark kar|check|check box|checkbox|tick|press|click|click kar|"
     r"tap|daba|submit|select", "click_element"),
    # read / summarize page
    (r"read|extract|text|summar|what.?s on the page|content|parh|bol",
     "extract_page_text"),
    # captcha / wall
    (r"captcha|verify|wall|challenge|just a moment|turnstile",
     "detect_captcha_boxes"),
    # errors
    (r"error|wrong|problem|kya galti|fail|issue", "get_errors"),
    # form introspection (what fields exist?)
    (r"form", "extract_form_context"),
    # layout / where is everything
    (r"layout|where|describe|map|box|position|element", "describe_page"),
    (r"verify|confirm|landed|clicked", "verify_click"),
]


def match_tool(instruction: str) -> str | None:
    """Return the registry tool name this instruction maps to (or None)."""
    low = instruction.lower()
    for pattern, name in TOOL_MATCHES:
        if re.search(pattern, low):
            return name
    return None


def extract_fields(instruction: str) -> dict:
    """Best-effort parse of 'field=value' pairs present in the instruction.

    Handles common phrasings:
        name=LO, email=x@y.com
        name: LO email: x@y.com
        set name to LO and email to x@y.com
    We require the key to be a recognizable field word (letters/digits) and
    the VALUE to NOT itself be 'key=value' (so we don't eat the whole tail).
    """
    fields = {}
    # normalize 'set X to Y' too (also split remaining chained 'and ... to')
    instr = re.sub(r"\bset\s+([A-Za-z_]\w*)\s+to\s+", lambda m: f"{m.group(1)}=",
                   instruction)
    instr = re.sub(r"\band\s+([A-Za-z_]\w*)\s+to\s+", lambda m: f"{m.group(1)}=",
                   instr)
    # field=value or field: value — stop value at comma / semicolon / another
    # 'word=' that begins a new pair.
    pat = re.compile(r"(?<![A-Za-z0-9])([A-Za-z_][A-Za-z0-9_]*)\s*[=:]\s*"
                     r"([^,;]+?)(?=\s+(?:[A-Za-z_][A-Za-z0-9_]*)\s*[=:]|$)")
    for m in pat.finditer(instr):
        k, v = m.group(1).strip(), m.group(2).strip().rstrip(".,")
        fields[k] = v
    return fields


def extract_selector(instruction: str) -> str | None:
    """Find a CSS-selector-looking token: #id, .class, tagname, [attr=..].

    Avoids matching bare single letters like 'a' inside random words.
    """
    m = re.search(
        r"(#[A-Za-z][\w-]*|\.\.[A-Za-z][\w-]*|\[[A-Za-z]+=[^\]]+\]|"
        r"(?:button|input|textarea|select|a|form|div)\b[\w.#\-\[\]=]*)",
        instruction, re.I)
    if not m:
        return None
    tok = m.group(0)
    # drop a standalone 'a ' (article) — keep only real element detections
    if tok.strip().lower() == "a":
        return None
    return tok.strip()


# ---------------------------------------------------------------------------
# mock model (offline stand-in for the LLM's 'which tool + args?' answer)
# ---------------------------------------------------------------------------


def mock_model(instruction: str) -> dict:
    """Pretend to be the LLM: decide tool + args for an instruction WITHOUT any
    API key. Returns {"tool": name, "args": {...}} — the exact shape a real
    model's tool-call / JSON reply would produce, so the agent loop is
    exercised identically in offline tests and with a live key.
    """
    tool = match_tool(instruction)
    args = {}
    if tool == "fill_form":
        args["fields"] = extract_fields(instruction)
    elif tool in ("click_element", "verify_click"):
        sel = extract_selector(instruction)
        if sel:
            args = {"sel": sel} if tool == "click_element" else \
                {"sel": sel}
    return {"tool": tool, "args": args}


# ---------------------------------------------------------------------------
# AgentLoop — the loop that actually drives the page
# ---------------------------------------------------------------------------


class AgentLoop:
    """Execute tool calls against a real page. Two decide-paths:

      * decide='offline'  — use mock_model (no key needed). Great for testing.
      * decide='model'    — call the real LLM via AiAssistant.chat() and ask
                            it for JSON {tool, args}. Needs an API key.
      * decide='auto'     — match tools by keywords locally AND, when an
                            api_key is configured and local match is weak,
                            ask the model. (default)

    The chosen tool is resolved from the registry and run with the bound page.
    """

    def __init__(self, assistant: AiAssistant | None = None,
                 decide: str = "auto"):
        self.assistant = assistant
        self.decide = decide
        self.registry = BrowserTools.registry()
        self.trace = []          # list of (instruction, tool, args, result)

    # -- decision -----------------------------------------------------------
    def _decide(self, instruction: str) -> dict:
        if self.decide == "offline" or (
                self.decide == "auto" and not self._has_key()):
            return mock_model(instruction)
        # try local match first, fall back to model for 'auto'
        tool = match_tool(instruction)
        args = {}
        if tool == "fill_form":
            args["fields"] = extract_fields(instruction)
        elif tool in ("click_element", "verify_click"):
            sel = extract_selector(instruction)
            if sel:
                args["sel"] = sel
        if "auto" in self.decide and (not tool or self._has_key()):
            # ask the real model for a JSON {tool, args} decision
            try:
                resp = self.assistant.chat([
                    {"role": "system",
                     "content": "You drive a browser. Reply with ONLY a JSON "
                                "object {\"tool\": ..., \"args\": {...}} using "
                                "tools: " + json.dumps(list(self.registry))},
                    {"role": "user", "content": instruction},
                ], max_tokens=150)
                parsed = json.loads(resp)
                return parsed
            except Exception:
                # model unavailable -> fall back to local match
                pass
        return {"tool": tool, "args": args}

    def _has_key(self) -> bool:
        if not self.assistant:
            return False
        cfg = self.assistant.config
        return bool(cfg and cfg.get("api_key"))

    # -- execute ------------------------------------------------------------
    def _run_tool(self, tool: str, args: dict, page):
        if tool is None or tool not in self.registry:
            return {"error": f"unknown tool {tool!r}; known: "
                             f"{list(self.registry)}"}
        entry = self.registry[tool]
        fn = entry["fn"]
        try:
            result = fn(page, **(args or {}))
        except Exception as e:
            result = {"error": f"{tool} raised: {str(e)[:240]}"}
        return result

    # -- the loop -----------------------------------------------------------
    def run(self, page, instruction: str) -> dict:
        """One agent turn: instruction -> decide tool -> run it -> trace.

        Returns a dict the caller (or a model) can read:
            {instruction, tool, args, result, trace}
        """
        decision = self._decide(instruction)
        tool = decision.get("tool")
        args = decision.get("args") or {}
        result = self._run_tool(tool, args, page)
        step = {"instruction": instruction, "tool": tool, "args": args,
                "result": result}
        self.trace.append(step)
        return {"instruction": instruction, "tool": tool, "args": args,
                "result": result, "trace": self.trace}


# ---------------------------------------------------------------------------
# convenience: one-shot loop
# ---------------------------------------------------------------------------


def run_agent_turn(page, instruction: str, assistant: AiAssistant | None = None,
                   decide: str = "auto") -> dict:
    """Fill a page then verify: 'Quan, form fill kar de...' style instruction to
    actual browser actions. Returns the run dict (see AgentLoop.run)."""
    loop = AgentLoop(assistant, decide=decide)
    return loop.run(page, instruction)
