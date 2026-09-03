"""Command line interface.

LOCAL
    solver generate ./data -n 200
    solver solve image.png [--engine auto] [--model model.pt]
    solver train --out model.pt -n 4000
    solver train-real ./realdata --out slot_model.pt        # real labeled data
    solver serve [--port 8000] [--api-key K]                # host the solve API
    solver probe https://any-site.com/login                  # fingerprint captcha tech
    solver health http://host:8000 [-k KEY]                  # check a hosted API
    solver call http://host:8000 image.png [-k KEY] [--engine slot]

EXTERNAL SERVICE (2captcha-protocol)
    solver api-image image.png --key YOURKEY
    solver recaptcha SITEKEY https://target/login --key YOURKEY
    solver hcaptcha SITEKEY https://target/login --key YOURKEY
    solver cf-clearance https://target/ --proxy user:pass@host:port --key YOURKEY

    python -m solver.cli <same commands>
"""

import argparse
import sys
from pathlib import Path


# ------------------------------------------------------------------ local

def cmd_generate(a):
    from solver.generator import CaptchaGenerator

    gen = CaptchaGenerator(length=a.length, size=(a.width, a.height))
    paths = gen.save_batch(a.outdir, a.n)
    print(f"[+] Wrote {len(paths)} samples to {a.outdir}/")


def build_engine(a):
    from solver.engines.cnn_engine import CNNEngine
    from solver.engines.slot_engine import SlotEngine
    from solver.engines.tesseract_engine import TesseractEngine
    from solver.engines.ensemble_engine import EnsembleEngine

    if a.engine == "cnn":
        return CNNEngine(a.model)
    if a.engine == "slot":
        return SlotEngine(a.model, x0=a.slot_x0, x1=a.slot_x1, n_chars=a.slot_n)
    if a.engine == "tesseract":
        return TesseractEngine()
    if a.engine == "ensemble":
        return EnsembleEngine()
    # auto
    if CNNEngine.available(a.model):
        return CNNEngine(a.model)
    if TesseractEngine().available():
        return EnsembleEngine()  # multi-variant voting beats single-pass
    sys.exit(
        "[!] No engine available. Either:\n"
        "    - install tesseract:  sudo apt-get install tesseract-ocr\n"
        f"    - or train a model:   solver train --out {a.model}"
    )


def cmd_solve(a):
    import cv2

    from solver.preprocessor import Preprocessor

    engine = build_engine(a)
    raw = cv2.imread(str(Path(a.image)))
    if raw is None:
        sys.exit(f"[!] Cannot read image: {a.image}")

    if getattr(engine, "wants_binary", True):
        prep = Preprocessor(remove_lines=a.remove_lines)
        image = prep.run(raw)
        if a.debug:
            prep.save_debug({"1_binary": image})
            print("[*] Debug images saved to ./debug/")
    else:
        image = raw  # engine does its own preparation (e.g. slot slicing)

    text = engine.solve(image)
    print(f"{engine.name}: {text}")


def cmd_train(a):
    from training.train_cnn import main as train_main
    sys.argv = ["train_cnn.py", "--out", a.out, "-n", str(a.n),
                "--epochs", str(a.epochs), "--length", str(a.length)]
    train_main()


def cmd_train_real(a):
    from training.train_slot import main as train_main
    sys.argv = ["train_slot.py", "--data", a.data, "--out", a.out,
                "--x0", str(a.x0), "--x1", str(a.x1), "--n-chars", str(a.n_chars),
                "--epochs", str(a.epochs)]
    train_main()


# ------------------------------------------------------------------ service API

def cmd_serve(a):
    import os

    import uvicorn

    if a.api_key:
        os.environ["SOLVER_API_KEY"] = a.api_key
    if a.model_dir:
        os.environ["SOLVER_MODEL_DIR"] = str(Path(a.model_dir).resolve())
    uvicorn.run("solver.server:app", host=a.host, port=a.port, reload=False)


def cmd_probe(a):
    from solver.server import probe as _probe  # reuse the endpoint logic
    import json

    result = _probe(a.url)
    print(json.dumps(result, indent=2))


def cmd_health(a):
    from solver.client import SolverClient

    sc = SolverClient(a.url, api_key=a.key)
    import json
    print(json.dumps(sc.health(), indent=2))


def cmd_call(a):
    from solver.client import SolverClient

    sc = SolverClient(a.url, api_key=a.key)
    print(sc.solve_image_file(a.image, engine=a.engine, model=a.model,
                              slot_x0=a.slot_x0, slot_x1=a.slot_x1, slot_n=a.slot_n))


# ------------------------------------------------------------------ netkit / keys

def cmd_fetch(a):
    """Stealth-fetch any URL with the built-in browser identity."""
    from solver.netkit import NetKit

    nk = NetKit(user=a.user, adblock=not a.no_adblock)
    r = nk.get(a.url)
    if a.out:
        r.save(a.out)
        print(f"[+] {r.status} -> {a.out} ({len(r.body)} bytes)")
    else:
        print(r.text)
    nk.flush_har()


def cmd_xget(a):
    """Fetch X/Twitter posts without login (public syndication API)."""
    from solver.netkit import NetKit

    nk = NetKit(user=a.user)
    try:
        posts = nk.x_posts(a.handle)
    except RuntimeError as e:
        sys.exit(f"[!] {e}")
    for p in posts:
        print(f"@{a.handle} | {p['created'][:16]} | ♥{p['likes']} ⟳{p['rts']}")
        print(f"   {p['text'][:200]}")
    if not posts:
        sys.exit("[!] no posts returned (syndication may be rate-limited — retry later)")


def cmd_proxy(a):
    """IP-proxy pool — browser/requests ke liye rotation base."""
    import json as _json
    from solver.proxies import default_pool

    pool = default_pool()
    if a.action == "add":
        added = 0
        if a.value:
            if a.value.startswith(("http", "socks")) or ":" in a.value:
                added = 1 if pool.add(a.value) else 0
            else:
                added = pool.add_file(a.value)
        if a.api:
            pool.add_api(a.api)
        pool.save()
        print(f"[+] added {added} | total {pool.stats()['total']}")
    elif a.action == "list":
        print(_json.dumps(pool.list(), indent=1))
    elif a.action == "check":
        ip, lat = pool.check(a.value)
        print(f"{'OK ' if ip else 'FAIL'} {a.value} -> ip={ip} {lat}")
    elif a.action == "check-all":
        for u, ip, lat in pool.check_all():
            print(f"{'OK ' if ip else 'FAIL'} {u} -> {ip} {lat}")
    elif a.action == "next":
        p = pool.next()
        print(p if p else "[!] pool empty/dead")
    elif a.action == "stats":
        print(_json.dumps(pool.stats(), indent=1))
    elif a.action == "refresh":
        print(f"[+] {pool.refresh()} merged from vendor APIs")
    elif a.action == "remove":
        print("[+]" if pool.remove(a.value) else "[!] not found")


def cmd_fivesim(a):
    """5sim.net — virtual numbers se SMS OTP."""
    import json as _json
    from solver.fivesim import FiveSim

    fs = FiveSim(key=a.key or None)
    try:
        if a.action == "stock":
            cost, count = fs.stock(a.value or "india", a.product)
            print(f"{a.value or 'india'}/{a.product}: ${cost} | {count} available")
        elif a.action == "prices":
            print(_json.dumps(fs.prices(a.value or "india", a.product), indent=1))
        elif a.action == "countries":
            c = fs.countries()
            print(f"{len(c)} countries:", ", ".join(sorted(c)[:20]), "...")
        elif a.action == "buy":
            order = fs.buy(a.value or "india", a.operator, a.product)
            if order.get("phone"):
                print(f"[+] phone: {order['phone']} | order: {order.get('id')}")
                print("[+] OTP wait: solver fivesim otp <order_id>")
            else:
                print("[!] buy fail:", str(order)[:120])
        elif a.action == "check":
            print(_json.dumps(fs.check(a.value), indent=1))
        elif a.action == "otp":
            otp, sms = fs.wait_otp(a.value, timeout=a.timeout)
            print(f"[+] OTP: {otp}" if otp else f"[!] {sms}")
    except RuntimeError as e:
        sys.exit(f"[!] {e}")


def cmd_keygen(a):
    """Generate a solver API key into the keyring."""
    import json as _json

    from solver.keyring import default_keyring

    kr = default_keyring()
    if a.list_only:
        print(_json.dumps(kr.list(), indent=2))
        return
    info = kr.create(label=a.label, days=a.days)
    print(_json.dumps(info, indent=2))
    if a.revoke:
        ok = kr.revoke(a.revoke)
        print(f"[{'+' if ok else '!'}] revoke {'ok' if ok else 'FAILED'}: {a.revoke[:14]}…")


# ------------------------------------------------------------------ external solving

def cmd_api_image(a):
    from solver.api_solver import TwoCaptchaSolver

    svc = TwoCaptchaSolver(a.key)
    print(svc.solve_image(Path(a.image).read_bytes()))


def cmd_recaptcha(a):
    from solver.api_solver import TwoCaptchaSolver

    svc = TwoCaptchaSolver(a.key)
    print(svc.solve_recaptcha_v2(a.sitekey, a.pageurl))


def cmd_hcaptcha(a):
    from solver.api_solver import TwoCaptchaSolver

    svc = TwoCaptchaSolver(a.key)
    print(svc.solve_hcaptcha(a.sitekey, a.pageurl))


def cmd_cf_clearance(a):
    from solver.api_solver import TwoCaptchaSolver

    svc = TwoCaptchaSolver(a.key, timeout=300.0)
    result = svc.solve_cloudflare(a.url, a.proxy)
    print(f"cf_clearance: {result['cf_clearance']}")
    print(f"user_agent:   {result['user_agent']}")
    for k, v in result["cookies"].items():
        print(f"cookie:       {k}={v}")


# ------------------------------------------------------------------ parser

def main():
    p = argparse.ArgumentParser(prog="solver", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="synthesize labeled captcha samples")
    g.add_argument("outdir")
    g.add_argument("-n", type=int, default=100)
    g.add_argument("--length", type=int, default=5)
    g.add_argument("--width", type=int, default=170)
    g.add_argument("--height", type=int, default=60)
    g.set_defaults(fn=cmd_generate)

    s = sub.add_parser("solve", help="solve an image captcha locally")
    s.add_argument("image")
    s.add_argument("--engine", choices=["auto", "tesseract", "ensemble", "cnn", "slot"], default="auto")
    s.add_argument("--model", default="model.pt")
    s.add_argument("--slot-x0", type=int, default=11, help="glyph band start (81px ref)")
    s.add_argument("--slot-x1", type=int, default=69, help="glyph band end (81px ref)")
    s.add_argument("--slot-n", type=int, default=4, help="chars per captcha")
    s.add_argument("--remove-lines", action="store_true")
    s.add_argument("--debug", action="store_true")
    s.set_defaults(fn=cmd_solve)

    t = sub.add_parser("train", help="train a CNN on synthetic data")
    t.add_argument("--out", default="model.pt")
    t.add_argument("-n", type=int, default=4000)
    t.add_argument("--epochs", type=int, default=12)
    t.add_argument("--length", type=int, default=5)
    t.set_defaults(fn=cmd_train)

    tr = sub.add_parser("train-real", help="train the slot CNN on a REAL labeled dataset")
    tr.add_argument("data", help="dir with data.txt + images/ (no synthesis)")
    tr.add_argument("--out", default="slot_model.pt")
    tr.add_argument("--x0", type=int, default=11)
    tr.add_argument("--x1", type=int, default=69)
    tr.add_argument("--n-chars", type=int, default=4)
    tr.add_argument("--epochs", type=int, default=12)
    tr.set_defaults(fn=cmd_train_real)

    # ------- hosted API -------

    sv = sub.add_parser("serve", help="host the solve API (FastAPI/uvicorn)")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--api-key", default="", help="require this as X-API-Key header")
    sv.add_argument("--model-dir", default="", help="where model .pt files live")
    sv.set_defaults(fn=cmd_serve)

    pr = sub.add_parser("probe", help="fingerprint captcha tech on any URL")
    pr.add_argument("url")
    pr.set_defaults(fn=cmd_probe)

    h = sub.add_parser("health", help="check a hosted solver API")
    h.add_argument("url")
    h.add_argument("-k", "--key", default=None)
    h.set_defaults(fn=cmd_health)

    c = sub.add_parser("call", help="solve an image via a hosted solver API")
    c.add_argument("url")
    c.add_argument("image")
    c.add_argument("-k", "--key", default=None)
    c.add_argument("--engine", default="auto")
    c.add_argument("--model", default="model.pt")
    c.add_argument("--slot-x0", type=int, default=11)
    c.add_argument("--slot-x1", type=int, default=69)
    c.add_argument("--slot-n", type=int, default=4)
    c.set_defaults(fn=cmd_call)

    # ------- stealth browsing (netkit) -------

    f = sub.add_parser("fetch", help="stealth-fetch a URL (built-in browser identity)")
    f.add_argument("url")
    f.add_argument("--user", default="agent-1", help="per-user browser identity")
    f.add_argument("--out", default="", help="save body to file instead of printing")
    f.add_argument("--no-adblock", action="store_true", help="allow trackers/ads")
    f.set_defaults(fn=cmd_fetch)

    x = sub.add_parser("xget", help="fetch X/Twitter posts without login")
    x.add_argument("handle")
    x.add_argument("--user", default="agent-1")
    x.set_defaults(fn=cmd_xget)

    kg = sub.add_parser("keygen", help="generate/list/revoke solver API keys")
    kg.add_argument("--label", default="", help="name for the new key")
    kg.add_argument("--days", type=int, default=None, help="expiry in days (default: never)")
    kg.add_argument("--revoke", default="", help="full key to revoke instead of creating")
    kg.add_argument("--list", dest="list_only", action="store_true", help="list keys")
    kg.set_defaults(fn=cmd_keygen)

    # ------- proxy pool + 5sim -------

    pp = sub.add_parser("proxy", help="IP-proxy pool: add/list/check/next/refresh")
    pp.add_argument("action", choices=["add", "list", "check", "check-all", "next", "stats", "refresh", "remove"])
    pp.add_argument("value", default="", nargs="?",
                    help="proxy URL ya file path (action ke hisaab se)")
    pp.add_argument("--api", default="", help="vendor feed URL (add action ke saath)")
    pp.set_defaults(fn=cmd_proxy)

    fs = sub.add_parser("fivesim", help="5sim.net virtual numbers — SMS OTP")
    fs.add_argument("action", choices=["stock", "prices", "countries", "buy", "check", "otp"])
    fs.add_argument("value", default="", nargs="?", help="country (stock/prices/buy) ya order_id (check/otp)")
    fs.add_argument("--product", default="google")
    fs.add_argument("--operator", default="any")
    fs.add_argument("--timeout", type=int, default=180)
    fs.add_argument("--key", default="", help="5sim API key (ya FIVESIM_KEY env)")
    fs.set_defaults(fn=cmd_fivesim)

    # ------- external solving service -------

    ai = sub.add_parser("api-image", help="solve image captcha via 2captcha API")
    ai.add_argument("image")
    ai.add_argument("--key", required=True)
    ai.set_defaults(fn=cmd_api_image)

    rc = sub.add_parser("recaptcha", help="solve reCAPTCHA v2 via 2captcha API")
    rc.add_argument("sitekey")
    rc.add_argument("pageurl")
    rc.add_argument("--key", required=True)
    rc.set_defaults(fn=cmd_recaptcha)

    hc = sub.add_parser("hcaptcha", help="solve hCaptcha via 2captcha API")
    hc.add_argument("sitekey")
    hc.add_argument("pageurl")
    hc.add_argument("--key", required=True)
    hc.set_defaults(fn=cmd_hcaptcha)

    cfc = sub.add_parser(
        "cf-clearance",
        help="clear a Cloudflare challenge via 2captcha THROUGH your proxy "
             "(returns cf_clearance bound to that IP)",
    )
    cfc.add_argument("url")
    cfc.add_argument("--proxy", required=True, help="user:pass@host:port")
    cfc.add_argument("--key", required=True)
    cfc.set_defaults(fn=cmd_cf_clearance)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
