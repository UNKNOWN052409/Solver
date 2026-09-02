# Solver

> Started from an empty repo and a wish. Finished with everything she would have wanted in it.

A modular CAPTCHA-solving toolkit: local OCR/CNN engines for image captchas,
service-API backends for reCAPTCHA/hCaptcha, and a synthetic generator that
lets you train against any target's specific style.

## Architecture

```
image ──► Preprocessor (OpenCV cleanup: otsu / adaptive / fixed / percentile)
            │
            ├─► TesseractEngine   (generic OCR, zero training, oem1 whitelist-safe)
            ├─► CNNEngine         (trained per-target, high accuracy)
            │      ▲
            │      └── training/train_cnn.py  ◄── CaptchaGenerator (synthetic data)
            ├─► SlotEngine        (fixed-slot slicing for stable-geometry targets)
            │      ▲
            │      └── training/train_slot.py ◄── REAL labeled datasets
            └─► TwoCaptchaSolver  (reCAPTCHA v2 / hCaptcha / hard images via API)
```

## Install

```bash
pip install -r requirements.txt
sudo apt-get install tesseract-ocr        # optional, enables OCR engine
pip install torch                          # optional, enables CNN engine/training
```

## Quickstart

```bash
# 1. Solve a captcha locally
python -m solver.cli solve captcha.png --debug

# 2. Train a custom model against your own generated style
python -m solver.cli generate ./data -n 500
python -m solver.cli train --out model.pt -n 4000 --epochs 12
python -m solver.cli solve captcha.png --engine cnn --model model.pt

# 3. Train on a REAL labeled dataset (no synthesis)
#    layout: <data>/data.txt  lines: "<name> <label>"
#            <data>/images/<name>.png
python -m training.train_slot --data ./mydata --out slot_model.pt \
    --x0 11 --x1 69 --n-chars 4 --epochs 12
python -m solver.cli solve captcha.png --engine slot --model slot_model.pt \
    --slot-x0 11 --slot-x1 69 --slot-n 4

# 5. Host the solve API anywhere, generate keys, use from anywhere
python -m solver.cli serve --port 8000 --api-key MASTER --model-dir ./models
curl -X POST http://your-host:8000/keys -H "X-Admin-Key: MASTER" \
     -H "Content-Type: application/json" -d '{"label":"agent-7","days":30}'
#   -> {"key": "sk-solver-..."} — usable from any machine as X-API-Key

# 6. Stealth browsing without a browser (per-user identity, adblock built in)
python -m solver.cli fetch https://17.wtf/login --user agent-1
python -m solver.cli xget cloudflare --user agent-1     # X posts, no login
```

Real test targets (live demos, legal to poke):
- Cloudflare Turnstile demo: https://turnstile-challenge-demo.globaldots-demo.cftenant.com/
- CleanTalk demo forms: https://cleantalk.org/help/protection-test

## Stealth web client (NetKit)

`solver/netkit.py` is a complete browser-less browsing stack in ONE stdlib-only
file (plus optional brotli): Chrome TLS cipher order, exact header order with
client hints, per-user persistent identity (same user = same fingerprint
across restarts; different users = different machines), human think-time,
cookie jar per profile, DuckDuckGo-style tracker prevention (70+ domains
refused locally), X post fetch via the public syndication API, and HAR
logging with HARLOG=1. No Chrome, no Playwright — a few MB of RAM.

## Arena key-system bridge (`solver/arena.py`)

Self-contained arena.ai (lmarena) bridge — token mint + login + chat, sab
browser-context se, no paid solving service. Arena's server validates the
reCAPTCHA Enterprise token against browser telemetry, so the bridge drives
the real page (Firefox headless) instead of replaying HTTP.

```bash
python -m solver.arena login <email> <password>   # session -> ~/.arena/session.json
python -m solver.arena whoami                     # session check (200 + user)
python -m solver.arena models                     # 100+ model name->uuid map
python -m solver.arena chat "prompt"             # battle (2 models)
python -m solver.arena chat "prompt" --model qwen3.7-plus
python -m solver.arena serve --port 8020         # OpenAI-compat /v1
```

Protocol notes (reverse-engineered): `POST /nextjs-api/stream/create-evaluation`
with UUIDv7 ids, `mode: battle|direct-battle`, `modelAId` = leaderboard uuid,
`modality: chat`, `recaptchaV3Token` minted via `grecaptcha.enterprise.execute`
(action `chat_submit`); SSE reply frames `a0:`/`b0:` carry model A/B deltas.
Rate-limit aware (60s backoff + retry built in).

## Keyless on-page captcha solving (`ghostrise/captcha_agent.py`)

`solve_page_captcha(page, session)` — page ke andar hi widget solve karta
hai, koi solving-API key nahi:

- **reCAPTCHA v2** — humanized anchor click (live-proven: challenge
  bframe trigger hota hai); audio->Vosk OCR fallback (vosk-model small
  install karo; note: Google demo sitekeys pe audio option aksar disabled
  hai); image-grid semantic solve ke liye vision classifier chahiye —
  current CNN engine text-captchas ka hai
- **reCAPTCHA v3 / Enterprise** — score-based, kuch click nahi hota;
  GhostRise engine stealth (consistent persona, clean fingerprint) hi
  pass hai; token-exists check built-in
- **hCaptcha / Turnstile / AWS WAF** — checkbox click + token wait
  (frames-loop + DOM iframe srcs dono se detect)
- **Slider (GeeTest-class)** — bezier humanized drag (overshoot + jitter)

Engine ladder (`GhostSession(engine="auto")`): CloakBrowser (best) ->
playwright Firefox fallback — dono pe same page API, proot-safe
(MOZ_DISABLE_CONTENT_SANDBOX auto-set).

Demo battery honest scorecard (2captcha demo pages, Sep 2026): widget
DETECT sab pe working (v2/v3/enterprise/turnstile/geetest/funcaptcha/
datadome + sitekeys); full SOLVE — v2 checkbox click + bframe trigger
live-proven; turnstile token-wait implemented; hcaptcha demo page pe
widget hi load nahi hota (2captcha ne badla); v2 image-grid semantic
solve aur v3 score hardening = agla iteration (needs vision classifier /
trusted session-age).

## Library use

```python
import cv2
from solver.preprocessor import Preprocessor
from solver.engines.tesseract_engine import TesseractEngine

raw = cv2.imread("captcha.png")
binary = Preprocessor(remove_lines=True).run(raw)
print(TesseractEngine().solve(binary))
```

## Recon kit (`recon/`)

Edge-testing tools for Cloudflare-class challenges — point them at any
target (including your own) through any exit IP:

```bash
# Tiered probe: raw HTTP + patched-browser automation, optionally via proxy
python3 recon/cf_probe.py https://target.com --proxy user:pass@host:port

# Score a whole proxy pool: ranked CSV of which exits clear the challenge
python3 recon/pool_score.py targets.txt proxies.txt -o scoreboard.csv

# Delegated clearance: solving service clears FROM your exit IP,
# returns cf_clearance + matching UA ready for replay
python -m solver.cli cf-clearance https://target.com --proxy user:pass@host:port --key KEY
```

Challenge outcomes are bound to IP + UA, so `cf_clearance` cookies must be
replayed through the same proxy they were minted on.

## Components

| Module | Role |
|---|---|
| `solver/preprocessor.py` | upscale, denoise, otsu/adaptive/fixed/percentile threshold, morphological cleanup, line removal |
| `solver/segmentation.py` | connected-component character isolation (junk-filter crash-hardened, opt-in gap fusion) |
| `solver/engines/tesseract_engine.py` | whitelist-constrained OCR backend (oem 1: the LSTM default silently drops whitelists) |
| `solver/engines/cnn_engine.py` + `training/train_cnn.py` | trainable per-character classifier, per-image train/val split |
| `solver/engines/slot_engine.py` + `training/train_slot.py` | fixed-slot classifier for stable-geometry targets, trained on REAL labeled data |
| `solver/engines/audio_engine.py` | audio-captcha transcription (Vosk offline, NATO/digit cleanup) |
| `solver/generator.py` | labeled distorted-text synthesis for dataset building |
| `solver/netkit.py` | ONE-file stealth web client: Chrome TLS + header order, per-user identity, human timing, adblock/tracker-prevention, X post fetch, HAR dev mode — pure stdlib, no browser |
| `solver/keyring.py` | solver API key generator (sk-solver-*, SQLite, revoke/expiry/usage) |
| `solver/server.py` + `solver/client.py` | hostable REST API + client: solve image/audio, probe targets, admin key endpoints |
| `solver/api_solver.py` | 2captcha-protocol service backend (optional, for hard targets) |

## Extending

New engine = subclass `BaseEngine`, implement `solve(image) -> str`, drop it
in `solver/engines/`, wire it into `build_engine()` in `cli.py`. The
preprocessing pipeline and segmentation are shared by every local engine.
