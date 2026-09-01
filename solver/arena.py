"""Arena key-system — self-contained arena.ai (lmarena) bridge.

Ek hi module, teen cheezein (sab browser-context se, FREE — koi paid
solving service nahi):
  1. TOKEN MINT   — reCAPTCHA Enterprise v3 action-scoped token
                    (grecaptcha.enterprise.execute) page context me
  2. LOGIN        — email+password session (arena_session.json cookies)
  3. CHAT         — battle / direct-battle create-evaluation SSE
                    (har arena model: qwen, deepseek, gpt, claude...)

Kya add hota hai jo curl se nahi milta: arena ka server reCAPTCHA
token ka browser telemetry score + action scope verify karta hai.
Browser context (Firefox headless) me app ke apne JS se token mint
hoti hai — wahi 200 laata hai.

Usage:
    python -m solver.arena login <email> <password>
    python -m solver.arena whoami
    python -m solver.arena models                # name -> uuid map
    python -m solver.arena chat "prompt"          # battle (2 random)
    python -m solver.arena chat "prompt" --model qwen3.7-plus
    python -m solver.arena serve --port 8020      # OpenAI-compat /v1

Files:
    ~/.arena/session.json    — cookies + localStorage (login state)
    ~/.arena/models.json     — name->uuid leaderboard map (cache)
"""

import argparse
import json
import os
import re
import sys
import time

ARENA_HOME = os.path.expanduser("~/.arena")
SESSION_FILE = os.path.join(ARENA_HOME, "session.json")
MODELS_FILE = os.path.join(ARENA_HOME, "models.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) "
      "Gecko/20100101 Firefox/131.0")

FIREFOX_ENV = {"MOZ_DISABLE_CONTENT_SANDBOX": "1", "MOZ_DISABLE_GMP_SANDBOX": "1"}

CHAT_JS = """
async (args) => {
    const [prompt, modelUuid] = args;
    function uuidv7() {
        const ts = Date.now();
        const rnd = crypto.getRandomValues(new Uint8Array(10));
        const b = new Uint8Array(16);
        const dv = new DataView(b.buffer);
        dv.setUint16(0, Math.floor(ts / 2**32));
        dv.setUint32(2, ts % 2**32);
        b.set(rnd, 6);
        b[6] = (b[6] & 0x0f) | 0x70;
        b[8] = (b[8] & 0x3f) | 0x80;
        const h = [...b].map(x => x.toString(16).padStart(2,'0')).join('');
        return [h.slice(0,8),h.slice(8,12),h.slice(12,16),h.slice(16,20),h.slice(20,32)].join('-');
    }
    await new Promise(r => grecaptcha.enterprise.ready(r));
    const token = await grecaptcha.enterprise.execute(
        '6LeTGMcsAAAAALuIlkVwIxaAuZA8VledA6d3Nnb0', {action: 'chat_submit'});

    const body = {
        id: uuidv7(),
        mode: modelUuid ? 'direct-battle' : 'battle',
        ...(modelUuid ? {modelAId: modelUuid} : {}),
        userMessageId: uuidv7(),
        modelAMessageId: uuidv7(),
        userMessage: {content: prompt, experimental_attachments: [], metadata: {}},
        modality: 'chat',
        recaptchaV3Token: token
    };
    const r = await fetch('/nextjs-api/stream/create-evaluation', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        credentials: 'include',
        body: JSON.stringify(body)
    });
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let out = '';
    const deadline = Date.now() + 120000;
    while (Date.now() < deadline) {
        const {done, value} = await reader.read();
        if (done) break;
        out += dec.decode(value, {stream: true});
        if (out.length > 60000) break;
    }
    return {status: r.status, sse: out};
}
"""


def _launch():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    b = pw.firefox.launch(headless=True)
    return pw, b


def _load_cookies():
    if not os.path.exists(SESSION_FILE):
        return None
    return json.load(open(SESSION_FILE)).get("cookies", [])


def _new_page(b):
    ctx = b.new_context(user_agent=UA)
    ck = _load_cookies()
    if ck:
        ctx.add_cookies(ck)
    return ctx, ctx.new_page()


def _save_session(ctx, page):
    os.makedirs(ARENA_HOME, exist_ok=True)
    state = ctx.storage_state()
    ls = page.evaluate("() => JSON.stringify(Object.fromEntries(Object.entries(localStorage)))")
    json.dump({"cookies": state["cookies"], "localStorage": json.loads(ls)},
              open(SESSION_FILE, "w"), indent=2)


def _login_flow(page, email, password):
    r = page.evaluate("""async (args) => {
        const [email, password] = args;
        const r = await fetch('/nextjs-api/sign-in/email', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            credentials: 'include',
            body: JSON.stringify({email: email, password: password,
                                  shouldLinkHistory: true})
        });
        const t = await r.text();
        return {status: r.status, body: t.slice(0, 400)};
    }""", [email, password])
    if r.get("status") != 200:
        raise RuntimeError(f"login failed: {r.get('status')} {r.get('body', '')[:200]}")
    return r


def cmd_login(args):
    pw, b = _launch()
    try:
        ctx, page = _new_page(b)
        page.goto("https://arena.ai/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        _login_flow(page, args.email, args.password)
        _save_session(ctx, page)
        print("[+] login OK — session saved:", SESSION_FILE)
    finally:
        b.close(); pw.stop()


def cmd_whoami(_):
    pw, b = _launch()
    try:
        ctx, page = _new_page(b)
        page.goto("https://arena.ai/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        me = page.evaluate("""async () => {
            const r = await fetch('/api/me', {credentials: 'include'});
            return {status: r.status, body: (await r.text()).slice(0, 400)};
        }""")
        print(f"[me] {me['status']} {me['body']}")
    finally:
        b.close(); pw.stop()


def cmd_models(args):
    pw, b = _launch()
    try:
        ctx, page = _new_page(b)
        # leaderboard RSC payload se name -> uuid
        page.goto("https://arena.ai/leaderboard/text", wait_until="domcontentloaded",
                  timeout=60000)
        page.wait_for_timeout(5000)
        rsc = page.evaluate("""async () => {
            const r = await fetch('/leaderboard/text', {credentials: 'include',
                                                        headers: {'RSC': '1'}});
            return await r.text();
        }""")
        pairs = re.findall(
            r'"id":"(01[a-f0-9-]{31,36})"[^}]{0,200}?"publicName":"([^"]+)"', rsc)
        if not pairs:
            # DOM fallback
            rows = page.evaluate("() => document.body.innerText.slice(0, 8000)")
            names = re.findall(r"\n\d+\n\d+\n([a-z0-9._-]{3,40})\n", rows)
            pairs = [(n, "") for n in names]
        mapping = {name: pid for pid, name in pairs}
        os.makedirs(ARENA_HOME, exist_ok=True)
        json.dump(mapping, open(MODELS_FILE, "w"), indent=1)
        print(f"[+] {len(mapping)} models -> {MODELS_FILE}")
        for k in list(mapping)[:20]:
            print(f"    {k:32s} {mapping[k]}")
    finally:
        b.close(); pw.stop()


def _parse_sse(sse):
    """arena SSE frames: a0:"..." b0:"..." (model A/B content), ad:/bd: finish.
    Battle mode -> dono models jod ke return; direct -> sirf a0."""
    a = re.findall(r'^a0:"((?:[^"\\]|\\.)*)"', sse, re.M)
    b_ = re.findall(r'^b0:"((?:[^"\\]|\\.)*)"', sse, re.M)
    def unesc(s):
        return s.encode().decode("unicode_escape", errors="replace")
    return unesc("".join(a)), unesc("".join(b_))


def cmd_chat(args):
    if not os.path.exists(SESSION_FILE):
        sys.exit("[!] pehle login karo: python -m solver.arena login <email> <pass>")
    model_uuid = ""
    if args.model:
        if not os.path.exists(MODELS_FILE):
            cmd_models(argparse.Namespace())
        mapping = json.load(open(MODELS_FILE))
        model_uuid = mapping.get(args.model, "")
        if not model_uuid:
            close = [k for k in mapping if args.model.lower() in k.lower()]
            if close:
                model_uuid = mapping[close[0]]
                print(f"[*] model '{args.model}' -> {close[0]} ({model_uuid[:18]}...)")
            else:
                sys.exit(f"[!] model '{args.model}' nahi mila — models dekho")

    pw, b = _launch()
    try:
        ctx, page = _new_page(b)
        # UI-driven flow: app ka apna recaptcha context hi 200 laata hai
        # (direct fetch pe enterprise score reject hota hai). Fresh chat URL
        # pe message bhejo aur DOM se reply padho.
        page.goto("https://arena.ai/text/direct" if model_uuid else "https://arena.ai/",
                  wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        box = page.wait_for_selector("textarea:visible", timeout=15000)
        box.fill(args.prompt)
        page.wait_for_timeout(400)
        try:
            page.click("button[aria-label='Send message']:visible", timeout=3000)
        except Exception:
            page.keyboard.press("Enter")
        # reply ka wait — prompt ka echo aane do, phir uske baad ka text
        marker = args.prompt.strip()[-25:]
        deadline = time.time() + 150
        reply = ""
        while time.time() < deadline:
            page.wait_for_timeout(4000)
            txt = page.evaluate("() => document.body.innerText.slice(0, 20000)")
            if "rate limit" in txt.lower() or "rate-limit" in txt.lower():
                wait_s = 60
                print(f"[*] arena rate limit — {wait_s}s backoff...", flush=True)
                time.sleep(wait_s)
                # retry: same message phir se
                try:
                    box2 = page.wait_for_selector("textarea:visible", timeout=8000)
                    box2.fill(args.prompt)
                    page.click("button[aria-label='Send message']:visible", timeout=3000)
                except Exception:
                    pass
                deadline += wait_s
                continue
            if marker in txt:
                # prompt-echo ke baad ka text — agli line se
                idx = txt.rfind(marker)
                reply = txt[idx + len(marker):].strip()
                # streaming continue ho sakti hai — thoda aur wait karke
                time.sleep(6)
                txt2 = page.evaluate("() => document.body.innerText.slice(0, 20000)")
                idx2 = txt2.rfind(marker)
                reply = txt2[idx2 + len(marker):].strip()
                if reply:
                    break
        if not reply:
            page.screenshot(path=os.path.join(ARENA_HOME, "chat_fail.png"))
            print("[!] reply timeout — screenshot: ~/.arena/chat_fail.png")
            return
        # UI chrome (menus) hatao — sirf pehli 2000 chars
        reply = reply[:2000]
        print(reply)
    finally:
        b.close(); pw.stop()


# ---------------- OpenAI-compat server (/v1/chat/completions) ----------------

def cmd_serve(args):
    import threading
    import requests as rq
    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse

    app = FastAPI(title="arena-bridge")

    def run_chat(prompt, model):
        model_uuid = ""
        if model and model != "arena":
            if os.path.exists(MODELS_FILE):
                mapping = json.load(open(MODELS_FILE))
                model_uuid = mapping.get(model) or mapping.get(
                    next((k for k in mapping if model.lower() in k.lower()), ""), "")
        pw, b = _launch()
        try:
            ctx, page = _new_page(b)
            page.goto("https://arena.ai/text/direct" if model_uuid else "https://arena.ai/",
                      wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(6000)
            r = page.evaluate(CHAT_JS, [prompt, model_uuid])
            a, _ = _parse_sse(r.get("sse", "")) if r.get("status") == 200 else ("", "")
            return a or f"[arena error {r.get('status')}]"
        finally:
            b.close(); pw.stop()

    @app.get("/health")
    def health():
        return {"ok": True, "bridge": "arena", "logged_in": os.path.exists(SESSION_FILE)}

    @app.get("/v1/models")
    def models():
        data = [{"id": "arena", "object": "model"}]
        if os.path.exists(MODELS_FILE):
            data += [{"id": k, "object": "model"} for k in json.load(open(MODELS_FILE))]
        return {"object": "list", "data": data}

    @app.post("/v1/chat/completions")
    def completions(request: Request):
        import uuid as _u
        body = json.loads(asyncio_run(request))
        prompt = render_msgs(body.get("messages", []))
        mt = body.get("max_tokens") or 0
        if mt:
            prompt += f"\n\n[Output budget: thorough answer, roughly {mt} tokens. Don't stop early.]"
        result = run_chat(prompt, body.get("model", "arena"))
        return {
            "id": f"chatcmpl-{_u.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", "arena"),
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": result},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": len(prompt) // 4,
                      "completion_tokens": len(result) // 4,
                      "total_tokens": (len(prompt) + len(result)) // 4},
        }

    print(f"[arena-bridge] http://0.0.0.0:{args.port}/v1")
    uvicorn.run(app, host="0.0.0.0", port=args.port)


def asyncio_run(request):
    import asyncio
    return asyncio.get_event_loop().run_until_complete(request.body())


def render_msgs(msgs):
    if len(msgs) == 1 and msgs[0].get("role") == "user":
        return msgs[0].get("content", "")
    parts = []
    for m in msgs:
        r = m.get("role", "user")
        if r == "system":
            parts.append(f"[Instructions]: {m.get('content', '')}")
        elif r == "assistant":
            parts.append(f"[Previous reply]: {m.get('content', '')}")
        else:
            parts.append(f"[User]: {m.get('content', '')}")
    parts.append("Answer the LAST [User] message directly.")
    return "\n\n".join(parts)


def main():
    os.environ.update(FIREFOX_ENV)
    ap = argparse.ArgumentParser(prog="solver.arena",
                                 description="arena.ai key-system bridge")
    sub = ap.add_subparsers(dest="cmd", required=True)

    lg = sub.add_parser("login", help="email+password session save")
    lg.add_argument("email"); lg.add_argument("password")
    lg.set_defaults(fn=cmd_login)

    w = sub.add_parser("whoami", help="session check")
    w.set_defaults(fn=cmd_whoami)

    m = sub.add_parser("models", help="name->uuid map (leaderboard)")
    m.set_defaults(fn=cmd_models)

    c = sub.add_parser("chat", help="chat (battle ya specific model)")
    c.add_argument("prompt")
    c.add_argument("--model", default="", help="arena model name (qwen3.7-plus...)")
    c.set_defaults(fn=cmd_chat)

    s = sub.add_parser("serve", help="OpenAI-compat /v1 server")
    s.add_argument("--port", type=int, default=8020)
    s.set_defaults(fn=cmd_serve)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
