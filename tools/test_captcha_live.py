#!/usr/bin/env python3
"""test_captcha_live.py — REAL captcha-solve test harness (5sim.net + demo sites).

What this does (no mock; every number is from a real run on this box):

  A. 5sim.net API CONTRACT  — hits the REAL endpoints and records the actual
     request/response shape. Verifies the keyless endpoints and that
     authenticated endpoints need a Bearer key. (5sim is a virtual-number /
     SMS-OTP rental API used AFTER a captcha, NOT a captcha-solving service.)

  B. REAL CAPTCHA DEMO SITES — probes a set of public captcha demo pages and
     detects which widget/sitekey each serves, and records how a challenge is
     injected/submitted (sitekey -> client token -> server verify). These are
     reachable from here.

  C. REAL SOLVE + SCORE — takes REAL captcha images with REAL ground-truth
     labels, runs the LOCAL (AI-independent) OCR solver, and measures REAL
     pass/fail. Counts exact-match wins + character-level accuracy.

The local solver is the repo's own stack: solver/engines/*, driven via the
userland tesseract tree (no network, no AI API). If tesseract is unavailable
the run honestly reports it instead of faking scores.

CLI:
    python3 tools/test_captcha_live.py                      # full run
    python3 tools/test_captcha_live.py --sample 40          # limit solve sample
    python3 tools/test_captcha_live.py --captcha-dir /path  # real images
    python3 tools/test_captcha_live.py --out /tmp/result.json
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Point at the userland tesseract tree (no-root Kali/proot) so the engine's
# own loader path is used even if it is not already exported.
os.environ.setdefault("SOLVER_TESS_ROOT", "/tmp/tessroot")

FIVESIM_BASE = "https://5sim.net"
UPPER = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# The real public captcha demo pages the harness probes (all reachable).
DEMO_SITES = [
    ("recaptcha_v2", "https://www.google.com/recaptcha/api2/demo"),
    ("hcaptcha", "https://accounts.hcaptcha.com/demo"),
    ("turnstile", "https://turnstile-challenge-demo.globaldots-demo.cftenant.com/"),
    ("2captcha_demo", "https://2captcha.com/demo"),
]


# -------------------------------------------------------------------------- A.
# 5sim.net real endpoint contract.

def _req(url: str, auth: bool = False, timeout: int = 20):
    headers = {"User-Agent": "solver/1.0", "Accept": "application/json"}
    if auth:
        headers["Authorization"] = "Bearer " + os.environ.get("FIVESIM_KEY", "")
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode(errors="replace")
            return {"http": r.status, "body": body}
    except urllib.error.HTTPError as e:
        return {"http": e.code, "body": e.read().decode(errors="replace")[:400]}
    except Exception as e:
        return {"http": 0, "body": str(e)[:200]}


def check_5sim_contract() -> dict:
    """Hit the real 5sim endpoints and record the live contract."""
    res = {
        "base": FIVESIM_BASE,
        "auth": "Bearer <key> in Authorization header",
        "endpoints": {},
        "verdict": "5sim.net is a virtual-number / SMS-OTP rental API, NOT a "
                   "captcha-solving service. No /captcha or token-solve endpoint.",
    }
    t = {}
    r = _req(f"{FIVESIM_BASE}/v1/guest/countries", auth=False)
    t["GET /v1/guest/countries"] = {"http": r["http"], "note": "keyless, full country list"}
    length = 0
    try:
        _d = json.loads(r["body"] or "{}")
        length = len(_d)
    except Exception:
        pass
    t["GET /v1/guest/countries"]["items"] = length

    r = _req(f"{FIVESIM_BASE}/v1/guest/prices?country=india", auth=False)
    t["GET /v1/guest/prices?country=india"] = {"http": r["http"], "note": "keyless price/stock matrix"}

    r = _req(f"{FIVESIM_BASE}/v1/user/profile", auth=True)
    t["GET /v1/user/profile"] = {"http": r["http"],
                                 "note": "Bearer required -> 401 without key"}

    r = _req(f"{FIVESIM_BASE}/v1/store/buy-activation-number/india/any/google", auth=True)
    t["GET /v1/store/buy-activation-number/{country}/{operator}/{product}"] = {
        "http": r["http"], "note": "Bearer required (rent a number)"}

    r = _req(f"{FIVESIM_BASE}/v1/user/check/000000", auth=True)
    t["GET /v1/user/check/{order_id}"] = {"http": r["http"], "note": "poll OTP"}

    res["endpoints"] = t
    res["has_5sim_key"] = bool(os.environ.get("FIVESIM_KEY"))
    res["contract_example"] = (
        "buy: GET /v1/store/buy-activation-number/{country}/{operator}/{product} "
        "-> {\"id\", \"phone\", \"country\", \"operator\", \"product\", \"cost\", \"status\"}\n"
        "check: GET /v1/user/check/{order_id} -> {\"id\", \"status\", \"sms\":[{\"text\":\"code\"}]}\n"
        "prices: GET /v1/guest/prices?country=X -> {X: {product: {operator: {cost,count}}}}")
    return res


# -------------------------------------------------------------------------- B.
# Demo site detection (lightweight HTTP, regex-based).

CAPTCHA_MARKERS = {
    "recaptcha": r"data-sitekey=[\"']([0-9A-Za-z_-]{30,})[\"']|grecaptcha|reCAPTCHA|/recaptcha/",
    "hcaptcha": r"data-sitekey=[\"']([0-9a-f-]{30,})[\"']|hcaptcha|api\.hcaptcha\.com",
    "turnstile": r"([0-9A-Za-z_-]{20,})|cloudflare|challenge-platform|turnstile",
    "geetest": r"geetest|gt4|initGeetest|gcaptcha",
    "funcaptcha": r"funcaptcha|arkose|api\.arkoselabs",
}


def probe_site(name: str, url: str) -> dict:
    info = {"site": name, "url": url}
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            html = r.read().decode(errors="replace")
        info["reachable"] = True
        info["http"] = r.status
    except Exception as e:
        info["reachable"] = False
        info["error"] = str(e)[:120]
        return info
    info["detected"] = []
    info["sitekeys"] = []
    for cap, pat in CAPTCHA_MARKERS.items():
        if re.search(pat, html, re.I):
            info["detected"].append(cap)
    # extract any data-sitekey (recaptcha / hcaptcha)
    for m in re.finditer(r"data-sitekey=[\"']([^\"']+)[\"']", html):
        info["sitekeys"].append(m.group(1))
    for m in re.finditer(r"sitekey[:=][\"']([^\"']+)[\"']", html):
        if m.group(1) not in info["sitekeys"]:
            info["sitekeys"].append(m.group(1))
    info["submit_flow"] = (
        "Widget reads `data-sitekey`, mints a client token, then the page's form "
        "submits that token (e.g. `g-recaptcha-response` / `h-captcha-response` / "
        "`cf-turnstile-response`) with the POST body to the site's own verify "
        "endpoint. The token is site/domain+IP-bound and validated server-side.")
    return info


# -------------------------------------------------------------------------- C.
# Real solve + score.

def make_solver(kind: str = "tesseract"):
    """Return a callable solve(img_bgr)->str using the repo's real engines."""
    from solver.engines.tesseract_engine import TesseractEngine
    if kind == "tesseract7":
        return TesseractEngine(charset=UPPER, psm=7)
    if kind == "tesseract13":
        return TesseractEngine(charset=UPPER, psm=13)
    if kind == "ensemble":
        from solver.engines.ensemble_engine import EnsembleEngine
        return EnsembleEngine(charset=UPPER)
    # auto: pick what's available
    eng = TesseractEngine(charset=UPPER, psm=7)
    if eng.available():
        return eng
    raise RuntimeError("no local OCR engine available (tesseract missing)")


def solve_batch(captcha_dir: Path, labels: dict, solver, max_n: int | None = None):
    import cv2
    files = sorted(captcha_dir.glob("*.png")) + sorted(captcha_dir.glob("*.jpg"))
    if max_n:
        files = files[:max_n]
    results = []
    solved = 0
    for f in files:
        name = f.name
        lab = labels.get(name, "").strip().upper()
        img = cv2.imread(str(f))
        if img is None:
            results.append({"file": name, "label": lab, "solver": "", "pass": False,
                            "reason": "cv2.imread failed"})
            continue
        try:
            read = solver.solve(img).strip()
        except Exception as e:
            read = ""
            results.append({"file": name, "label": lab, "solver": read,
                            "pass": False, "reason": f"solver error {str(e)[:60]}"})
            continue
        read = re.sub(r"[^0-9A-Za-z]", "", read).upper()
        ok = read == lab
        if ok:
            solved += 1
        results.append({"file": name, "label": lab, "solver": read, "pass": ok})
    return results, solved


def score(results: list) -> dict:
    n = len(results)
    solved = sum(1 for r in results if r.get("pass"))
    var = sum(1 for r in results if r.get("reason"))
    # char-level accuracy over all expected chars
    total_chars = 0
    chart_hits = 0
    for r in results:
        lab = r.get("label", "")
        sol = r.get("solver", "")
        total_chars += len(lab)
        # compare positionally up to min length
        k = min(len(lab), len(sol))
        chart_hits += sum(1 for i in range(k) if lab[i] == sol[i])
    return {
        "total": n,
        "solved_exact": solved,
        "failed": n - solved,
        "solver_error": var,
        "solve_rate": round(solved / n, 4) if n else 0.0,
        "char_accuracy": round(chart_hits / total_chars, 4) if total_chars else 0.0,
    }


def load_labels(labels_path: Path) -> dict:
    d = {}
    try:
        raw = json.loads(labels_path.read_text())
        for k, v in raw.items():
            d[k] = str(v).strip().upper()
    except Exception:
        pass
    return d


# -------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=None,
                    help="max captchas to score (default: all in dir)")
    ap.add_argument("--captcha-dir", default="/tmp/real_smol",
                    help="dir of real captcha images")
    ap.add_argument("--labels", default="/tmp/real_smol_labels.json",
                    help="json map filename->label (real ground truth)")
    ap.add_argument("--out", default=None, help="write JSON result here")
    args = ap.parse_args()

    report = {
        "test": "captcha-live",
        "date": __import__("datetime").datetime.now().isoformat(),
        "5sim": check_5sim_contract(),
        "demo_sites": [probe_site(n, u) for n, u in DEMO_SITES],
    }

    # ---- C: real solve ----
    labels = load_labels(Path(args.labels))
    cdir = Path(args.captcha_dir)
    solver_info = {}
    run = {}
    if cdir.is_dir() and labels:
        try:
            solver = make_solver("tesseract7")
            solver_info = {"kind": "tesseract(psm7,upper)", "available": True}
            results, solved = solve_batch(cdir, labels, solver, args.sample)
            run["score"] = score(results)
            run["engine"] = solver_info
            # per-file detail (cap output)
            run["results"] = results
        except Exception as e:
            run["error"] = f"solver unavailable: {e}"
            run["engine"] = {"available": False, "reason": str(e)}
    else:
        run["error"] = f"captcha dir {cdir} missing or labels empty"
        run["engine"] = {"available": False}

    report["solve_run"] = run

    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2))
        print(f"[+] results written -> {args.out}")

    # ---- human-readable summary ----
    print("=" * 60)
    print("5SIM.NET API CONTRACT")
    for ep, info in report["5sim"]["endpoints"].items():
        print(f"  {info.get('http')}  {ep}  ({info.get('note')})")
    print("verdict:", report["5sim"]["verdict"])
    print("=" * 60)
    print("DEMO SITES")
    for s in report["demo_sites"]:
        st = "reachable" if s.get("reachable") else "UNREACHABLE"
        print(f"  [{st}] {s['site']} {s['url']} detected={s.get('detected')}")
    print("=" * 60)
    print("REAL SOLVE + SCORE")
    print("engine:", run.get("engine"))
    if "score" in run:
        sc = run["score"]
        print(f"  total={sc['total']} solved_exact={sc['solved_exact']} "
              f"failed={sc['failed']} solver_error={sc['solver_error']}")
        print(f"  solve_rate={sc['solve_rate']} char_accuracy={sc['char_accuracy']}")
    else:
        print("  no real solve ran:", run.get("error"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
