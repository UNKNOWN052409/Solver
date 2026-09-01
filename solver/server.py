"""Hostable solve API (FastAPI). Deploy anywhere, use from anywhere.

    python -m solver.serve              # auto-port + 24/7 supervisor (preferred)
    uvicorn solver.server:app --port 8000   # direct (fixed port)

Optional auth: set env SOLVER_API_KEY=... and send header X-API-Key.

Endpoints:
    GET  /health                    engine availability + loaded models
    POST /solve/image               multipart file upload -> text
    POST /solve/image64             {"image_b64": "...", "engine": "slot"}
    POST /solve/audio               {"audio_b64": "...", "src_ext": ".mp3"}
    GET  /probe?url=<target>        fingerprint captcha tech on any page
    GET  /x/handle/{handle}         X timeline without login (posts JSON)
    GET  /x/post/{id}?text=true     single X post by ID (AI-readable)
    POST /solve/service             2captcha-style external solver passthrough
                                     {"kind": "recaptcha|hcaptcha|image",
                                      "sitekey": "...", "pageurl": "...",
                                      "image_b64": "..."} + X-2Captcha-Key
"""

import base64
import os
import re
import subprocess
from pathlib import Path

import cv2
import numpy as np
import requests
from fastapi import Depends, FastAPI, Header, HTTPException, UploadFile
from pydantic import BaseModel

from .api_solver import TwoCaptchaSolver
from .engines.audio_engine import AudioEngine
from .preprocessor import Preprocessor

app = FastAPI(title="Solver API", version="1.1")

# ---------------------------------------------------------------- models

MODEL_DIR = Path(os.environ.get("SOLVER_MODEL_DIR", "."))


class Image64Request(BaseModel):
    image_b64: str
    engine: str = "auto"  # auto | tesseract | cnn | slot
    model: str = "model.pt"
    slot_x0: int = 11
    slot_x1: int = 69
    slot_n: int = 4
    remove_lines: bool = False


class AudioRequest(BaseModel):
    audio_b64: str
    src_ext: str = ""
    charset: str = "0123456789"


class ServiceRequest(BaseModel):
    kind: str  # recaptcha | recaptcha-v3 | recaptcha-enterprise | hcaptcha | image | cloudflare | turnstile
    sitekey: str = ""
    pageurl: str = ""
    image_b64: str = ""
    proxy: str = ""
    phrase: bool = False
    action: str = ""   # recaptcha v3/enterprise action scope (e.g. chat_submit)
    min_score: float = 0.4


# ---------------------------------------------------------------- auth

def require_key(x_api_key: str = Header(default="")):
    """Master key (env) OR any live key from the keyring DB."""
    master = os.environ.get("SOLVER_API_KEY", "")
    if master:
        if x_api_key == master:
            return True
    else:
        return True  # no master configured, no keyring -> open (dev mode)
    if x_api_key and _kr().verify(x_api_key):
        return True
    if master:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")
    return False


def require_admin(x_admin_key: str = Header(default="")):
    """Key-admin endpoints need the master key (env SOLVER_ADMIN_KEY)."""
    expected = os.environ.get("SOLVER_ADMIN_KEY", os.environ.get("SOLVER_API_KEY", ""))
    if not expected or x_admin_key != expected:
        raise HTTPException(status_code=401, detail="admin key required (SOLVER_ADMIN_KEY)")
    return True


def _kr():
    from .keyring import default_keyring
    return default_keyring()


# ---------------------------------------------------------------- engine mgmt

_engines = {}


def get_engine(name: str, model: str, slot_x0=11, slot_x1=69, slot_n=4):
    key = (name, model)
    if key in _engines:
        return _engines[key]
    if name == "tesseract":
        from .engines.tesseract_engine import TesseractEngine
        eng = TesseractEngine()
    elif name == "cnn":
        from .engines.cnn_engine import CNNEngine
        eng = CNNEngine(str(MODEL_DIR / model))
    elif name == "slot":
        from .engines.slot_engine import SlotEngine
        eng = SlotEngine(str(MODEL_DIR / model), x0=slot_x0, x1=slot_x1, n_chars=slot_n)
    else:
        raise HTTPException(status_code=400, detail=f"unknown engine: {name}")
    # CNN/Slot availability = model file loaded; Tesseract = binary present
    avail = getattr(eng, "instance_available", None)
    ok = avail() if avail is not None else eng.available()
    if not ok:
        raise HTTPException(status_code=503, detail=f"engine '{name}' unavailable (deps/model)")
    _engines[key] = eng
    return eng


def solve_image_bytes(data: bytes, engine="auto", model="model.pt",
                      slot_x0=11, slot_x1=69, slot_n=4, remove_lines=False):
    if not data:
        raise HTTPException(status_code=400, detail="empty image payload")
    try:
        arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    except cv2.error:
        arr = None
    if arr is None:
        raise HTTPException(status_code=400, detail="not a decodable image")
    if engine == "auto":
        # prefer tesseract if binary present, else cnn/slot if model exists
        from .engines.tesseract_engine import TesseractEngine
        eng_name = "tesseract" if TesseractEngine().available() else "cnn"
        if eng_name == "cnn" and not (MODEL_DIR / model).exists():
            raise HTTPException(status_code=503, detail="no tesseract binary and no model file")
        engine = eng_name
    eng = get_engine(engine, model, slot_x0, slot_x1, slot_n)
    if getattr(eng, "wants_binary", True):
        arr = Preprocessor(remove_lines=remove_lines).run(arr)
    return {"engine": eng.name, "text": eng.solve(arr)}


# ---------------------------------------------------------------- endpoints

@app.get("/health")
def health():
    tess = False
    try:
        from .engines.tesseract_engine import TesseractEngine
        tess = TesseractEngine().available()
    except Exception:
        pass
    models = sorted(p.name for p in MODEL_DIR.glob("*.pt")) if MODEL_DIR.is_dir() else []
    return {
        "ok": True,
        "engines": {
            "tesseract": tess,
            "cnn": bool(models),
            "slot": bool(models),
            "audio": AudioEngine().available(),
            "service": True,  # TwoCaptcha passthrough always wired
        },
        "models": models,
        "model_dir": str(MODEL_DIR),
        "auth": bool(os.environ.get("SOLVER_API_KEY", "")),
    }


# ---------------------------------------------------------------- X reading

# Login-free X readers exposed as REST so any AI agent can consume them
# with plain HTTP. Backed by the live-verified endpoints:
#   timeline: syndication.twitter.com __NEXT_DATA__ (+ fallbacks)
#   single:  cdn.syndication.twimg.com/tweet-result (JSON, no login)
# The GhostMouse Rust binary is the engine when available (it owns the
# fallback chain); this is the HTTP face for agents.

GHOSTMOUSE = os.environ.get("GHOSTMOUSE_BIN", "target/release/ghostmouse")


def _gm_raw(*args, timeout=90):
    """Run ghostmouse, return raw stdout text (no JSON parse)."""
    binpath = _gm_bin()
    try:
        r = subprocess.run([binpath, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="ghostmouse timed out")
    if r.returncode != 0:
        raise HTTPException(status_code=502, detail=r.stderr.strip()[:300] or "empty output")
    return r.stdout.strip()


def _gm(*args, timeout=90):
    """Run ghostmouse CLI, return parsed JSON (or raise with stderr)."""
    import json as _json
    binpath = _gm_bin()
    try:
        r = subprocess.run([binpath, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="ghostmouse timed out")
    out = r.stdout.strip()
    if r.returncode != 0 or not out:
        raise HTTPException(status_code=502, detail=r.stderr.strip()[:300] or "empty output")
    try:
        return _json.loads(out)
    except ValueError:
        raise HTTPException(status_code=502, detail="unparseable ghostmouse output")


def _gm_bin():
    binpath = GHOSTMOUSE if os.path.exists(GHOSTMOUSE) else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "target", "release", "ghostmouse")
    if not os.path.exists(binpath):
        raise HTTPException(status_code=503, detail="ghostmouse binary not built")
    return binpath


@app.get("/x/handle/{handle}", dependencies=[Depends(require_key)])
def x_handle(handle: str, limit: int = 20):
    """Timeline for a handle: GET /x/handle/elonmusk?limit=20"""
    return {"ok": True, "handle": handle, "posts": _gm("x", handle, str(limit))}


@app.get("/x/post/{post_id}", dependencies=[Depends(require_key)])
def x_post(post_id: str, text: bool = False):
    """Single post by ID: GET /x/post/2094130588047266206?text=true"""
    if text:
        # --text prints bare prose (not JSON) — return it as a string
        out = _gm_raw("x-post", post_id, "--text")
        return {"ok": True, "text": out}
    return {"ok": True, "post": _gm("x-post", post_id)}


@app.post("/solve/image", dependencies=[Depends(require_key)])
def solve_image(file: UploadFile, engine: str = "auto", model: str = "model.pt",
                slot_x0: int = 11, slot_x1: int = 69, slot_n: int = 4):
    data = file.file.read()
    return solve_image_bytes(data, engine, model, slot_x0, slot_x1, slot_n)


@app.post("/solve/image64", dependencies=[Depends(require_key)])
def solve_image64(req: Image64Request):
    data = base64.b64decode(req.image_b64)
    return solve_image_bytes(
        data, req.engine, req.model,
        req.slot_x0, req.slot_x1, req.slot_n, req.remove_lines,
    )


@app.post("/solve/audio", dependencies=[Depends(require_key)])
def solve_audio(req: AudioRequest):
    eng = AudioEngine(charset=req.charset)
    if not eng.available():
        raise HTTPException(status_code=503, detail="audio engine unavailable: pip install vosk + model dir")
    data = base64.b64decode(req.audio_b64)
    return {"engine": "audio", "text": eng.solve(data, src_ext=req.src_ext)}


@app.get("/probe", dependencies=[Depends(require_key)])
def probe(url: str):
    """Fingerprint what captcha tech a page uses (sitekeys, widget types)."""
    try:
        r = requests.get(
            url, timeout=20,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"},
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"fetch failed: {e}")
    html = r.text
    found = {
        "url": url,
        "status": r.status_code,
        "tech": [],
        "sitekeys": {},
        "notes": [],
    }
    ts_keys = re.findall(r'data-sitekey="([^"]+)"[^>]*', html)
    ts_keys += re.findall(r"turnstileSitekey\s*=\s*['\"]([^'\"]+)", html)
    rec_keys = re.findall(r'data-sitekey="([^"]+)"', html)
    hc_keys = re.findall(r'data-sitekey="([^"]+)"', html)
    # JS-injected keys: SvelteKit/Next/Nuxt embed sitekeys in env objects,
    # fetch configs, and inline render calls — not just data-sitekey attrs.
    js_keys = re.findall(r"""["'][A-Z_]*(?:HCAPTCHA|RECAPTCHA|TURNSTILE)[A-Z_]*["']\s*[:=]\s*["']([^"']+)["']""", html)
    js_keys += re.findall(r"sitekey\s*[:=]\s*[\"']([0-9a-zA-Z_-]{20,})[\"']", html, re.I)
    js_keys += re.findall(r"sitekey[\"']?\s*[:=]\s*[\"']([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})[\"']", html, re.I)

    # image captchas: raw <img> pointing at a captcha endpoint
    img_captcha = re.findall(r'<img[^>]+src="([^"]*captcha[^"]*)"', html, re.I)

    if "challenges.cloudflare.com/turnstile" in html or ts_keys:
        found["tech"].append("cloudflare-turnstile")
        found["sitekeys"]["turnstile"] = sorted(set(ts_keys))
    # reCAPTCHA v3/Enterprise: script render=KEY + grecaptcha.enterprise
    # inline execute calls (arena-style: execute('KEY', {action: '...'}))
    v3_keys = re.findall(r"recaptcha(?:/enterprise)?\.js\?render=([0-9A-Za-z_-]{20,})", html)
    ent_exec = re.findall(r"grecaptcha\.enterprise\.execute\(\s*['\"]([0-9A-Za-z_-]{20,})['\"]", html)
    plain_exec = re.findall(r"grecaptcha\.execute\(\s*['\"]([0-9A-Za-z_-]{20,})['\"]", html)
    if "google.com/recaptcha" in html or "g-recaptcha" in html or "grecaptcha" in html:
        found["tech"].append("recaptcha")
        found["sitekeys"]["recaptcha"] = sorted(set(rec_keys + v3_keys + plain_exec))
    if "grecaptcha.enterprise" in html or v3_keys and "enterprise" in html:
        found["tech"].append("recaptcha-enterprise")
        found["sitekeys"]["recaptcha-enterprise"] = sorted(set(v3_keys + ent_exec))
    # action scopes from execute calls (arena: chat_submit, create_evaluation)
    actions = re.findall(r"grecaptcha(?:\.enterprise)?\.execute\([^)]*?\{\s*action:\s*['\"]([a-z_]+)['\"]", html)
    if actions:
        found["actions"] = sorted(set(actions))
    if "hcaptcha.com" in html or "h-captcha" in html or "hcaptcha_token" in html:
        found["tech"].append("hcaptcha")
        found["sitekeys"]["hcaptcha"] = sorted(set(hc_keys + [k for k in js_keys if "-" in k and len(k) == 36]))
    if not found["tech"]:
        found["notes"].append("no captcha markers in static HTML (may be JS-injected)")
    if js_keys:
        found["sitekeys"]["js_injected"] = sorted(set(js_keys))
    if img_captcha:
        found["tech"].append("image-captcha")
        found["image_endpoints"] = img_captcha[:5]
    return found


@app.post("/solve/service", dependencies=[Depends(require_key)])
def solve_service(req: ServiceRequest, x_2captcha_key: str = Header(default="")):
    """Passthrough to an external solving service (2captcha protocol)."""
    if not x_2captcha_key:
        raise HTTPException(status_code=400, detail="X-2Captcha-Key header required")
    svc = TwoCaptchaSolver(x_2captcha_key)
    try:
        if req.kind == "recaptcha":
            return {"kind": req.kind, "token": svc.solve_recaptcha_v2(req.sitekey, req.pageurl)}
        if req.kind == "recaptcha-v3":
            return {"kind": req.kind, "token": svc.solve_recaptcha_v3(
                req.sitekey, req.pageurl, action=req.action, min_score=req.min_score)}
        if req.kind == "recaptcha-enterprise":
            return {"kind": req.kind, "token": svc.solve_recaptcha_enterprise(
                req.sitekey, req.pageurl, action=req.action, min_score=req.min_score)}
        if req.kind == "turnstile":
            return {"kind": req.kind, "token": svc.solve_turnstile(req.sitekey, req.pageurl)}
        if req.kind == "hcaptcha":
            return {"kind": req.kind, "token": svc.solve_hcaptcha(req.sitekey, req.pageurl)}
        if req.kind == "image":
            data = base64.b64decode(req.image_b64)
            return {"kind": req.kind, "text": svc.solve_image(data, req.phrase)}
        if req.kind == "cloudflare":
            return {"kind": req.kind, **svc.solve_cloudflare(req.pageurl, req.proxy)}
    except (RuntimeError, TimeoutError, ValueError) as e:
        raise HTTPException(status_code=502, detail=str(e))
    raise HTTPException(status_code=400, detail=f"unknown kind: {req.kind}")


# ---------------------------------------------------------------- key admin
# Key generator: create solver API keys usable anywhere this API is hosted.
#   curl -X POST http://host:8000/keys -H "X-Admin-Key: ..." \
#        -d '{"label":"agent-7","days":30}'        -> {"key": "sk-solver-..."}
#   curl http://host:8000/keys -H "X-Admin-Key: ..."
#   curl -X DELETE http://host:8000/keys/<sk-solver-...> -H "X-Admin-Key: ..."

class KeyCreateRequest(BaseModel):
    label: str = ""
    days: int | None = None  # None = never expires


@app.post("/keys", dependencies=[Depends(require_admin)])
def create_key(req: KeyCreateRequest):
    return _kr().create(label=req.label, days=req.days)


@app.get("/keys", dependencies=[Depends(require_admin)])
def list_keys():
    return _kr().list()


@app.delete("/keys/{key}", dependencies=[Depends(require_admin)])
def revoke_key(key: str):
    if not _kr().revoke(key):
        raise HTTPException(status_code=404, detail="key not found (or wrong full key)")
    return {"revoked": key[:14] + "…"}
