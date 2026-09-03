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

## Keyless vision classifier (`solver/vision/`)

~6M-param TileNet stack — v2/hCaptcha grid tiles keyless solve karne
ka vision brain (koi solving API nahi):

- `model.py` — TileNet multi-label (97-class closed vocab) + RotNet
  (Arkose rotation, 36x10deg bins); numpy-ref + torch-trainable variants
- `harvest.py` — demo pages se grids harvest (prompt + tiles), zero
  manual labeling (weak-labels se distill)
- `train.py` — synthetic smoke (pipeline proof, live: loss 0.32->0.15)
  + torch path (BCE multi-label, ONNX + int8 export target)
- `serve.py` — FastAPI /classify + /rotate (live: 9-tile batch 703ms
  numpy-ref; ONNX int8 target ~10ms CPU, GPU batch ~50k tiles/sec/card)

Serving math (int8 ONNX): CPU core ~100-300 tiles/sec -> ~30-50ms/grid;
one GPU card batch(64) ~50k tiles/sec -> ~5k grids/sec -> few cards
serve 100k+ bursty users.

## Anti-captcha browser (`ghostrise/ac_browser.py`)

`ACSession` — GhostSession ke upar wrapper jo har page ko
"anti-captcha" banata hai. Agent code captcha ka dhyan rakhta hi nahi:

```python
from ghostrise.ac_browser import ACSession
with ACSession(profile="work1") as b:
    page = b.open("https://walled-site.com/login")   # wall/captcha auto
    print(b.last_info)  # {wall: cleared/none, captcha: kind: result}
```

Ladder har URL pe: load -> JS-interstitial wall wait/reload (Just a
moment / Verifying your browser class) -> captcha widget auto-solve
(keyless stack: stealth clicks, OCR, vision hook) -> verify -> retry
(max_retries). `b.last_info` me har attempt ka hisaab milta hai.

X stack (ghostrise/x_agent.py): x_posts timeline (browser fallback jab
Rust syndication chain IP-block ho), x_post single, x_search — honest
note: public nitter mirrors ne SEARCH pe automated-verification wall
lagaya hai (Sep 2026) jo CloakBrowser se bhi clear nahi hoti abhi;
parser + wall-clear code ready hai, mirror khulega to live. Timeline
chain (Rust: syndication -> xcancel RSS -> nitter) fully live.

## Multi-session + power-efficient (Kali-on-Android/proot)

3-4 parallel Hermes sessions ka budget math (Termux 32-child cap):
har session ~4-5 procs (hermes + kernel + mcp_stdio_watchdog +
mcp-proxy) -> 3 sessions = ~14, 4 = ~19; browsers 8+ EACH — isliye
browser tests ke baad hamesha cleanup. Do guards hain:

```bash
python3 tools/capwatch.py          # live budget report (procs/headroom)
python3 tools/capwatch.py --auto   # headroom <8 -> browsers cleanup, warna no-op
python3 tools/procguard.py --kill  # full test-proc cleanup (protected safe)
```

- `capwatch.py` — power-efficient: koi daemon/polling nahi, cron me
  5-min pe `--auto` (sirf zaroorat pe kaam). PROTECTED: hermes stack,
  burp-mcp, searxng, prexzy, kanban — kabhi touch nahi hota.
- `procguard.py` — test zombi cleanup (browsers, uvicorn, revd,
  mitmdump, mocks) with same protected list.
- Watchdog cron 1min -> **5min** (battery 5x, daemons 5-min max lag
  tolerate karte hain).
- Dead kanban workers: `hermes sessions archive --older-than 10h
  --title kanban --yes` (reversible soft-hide) — DB list clean.
- Rule of thumb: 4 sessions chal rahe hon to browser test pehle
  capwatch dekho; 2 se kam headroom = pehle cleanup phir browser.

## Screen-off survival + session-kill prevention (`tools/keepalive.sh`)

```bash
bash tools/keepalive.sh start    # ON (idempotent) — ab har session ke saath
bash tools/keepalive.sh status   # heartbeat age
bash tools/keepalive.sh stop
```

proot me Termux wake-lock root ke bina direct nahi milta — isliye
3-layer ladder:
1. **Heartbeat daemon** (nice-19): har 30s touch — ProcessRecord
   active rehta hai, phantom-kill delay. CPU ~0 (sleep 29 nibble —
   benchmark-verified negligible).
2. **Watchdog revive**: 5-min cron pe heartbeat >120s stale -> restart.
3. **@reboot crontab**: reboot pe auto-start.

One-time Termux-side (strongest layer, manual): Termux app me
**Termux:Widget** ka "Acquire wake lock" toggle ON + Android Settings >
Battery > Termux > **Unrestricted**. Phir ye proot layer backup hai.

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

## 5sim.net web captcha (03-Sep-2026, live-tested)

- Login pe **Cloudflare Turnstile managed-mode** (React, invisible —
  checkbox nahi hota). Signup/landing pe captcha nahi.
- Hamara stack: DETECT ✓ (turnstile widget + frame), turnstile obj ✓,
  humanized pre-interaction ✓, token-scan (inputs + fiber) ✓.
- **Honest wall**: managed Turnstile token-mint IP-reputation pe
  hota hai — ye host (datacenter/proot) pe CF mint refuse karta hai.
  20-30s settle + human warmup ke baad bhi token nahi.
- **Fix path (in-repo)**: `GhostSession(proxy="pool")` — residential
  proxy se launch karo, managed Turnstile wahan mint karta hai.
  Proxy pool (`solver/proxies.py`) isi use-case ke liye built hai.
- 5sim API pe captcha NAHI hai (Bearer key) — buy/OTP direct API
  se hota hai, web login ki zaroorat hi nahi.

## GhostWire — APNA engine layer (raw CDP, playwright-free) + WireMouse

Solver ka ab apna protocol layer hai (engine-layer in-house):

- `ghostrise/wire.py` — **GhostWire**: chromium ko raw CDP (WebSocket)
  se drive karta hai — zero playwright dependency, zero library
  CDP-call-pattern fingerprint. Stealth init-script inject
  (webdriver=undefined, chrome obj, plugins, languages), DOM/evaluate
  surface, resi/pool proxy support (--proxy-server).
- `ghostrise/wire_mouse.py` — **WireMouse**: raw `Input.dispatchMouseEvent`
  injection — bezier trajectories + perpendicular control-point
  curvature + micro-jitter + humanized press-duration. `Input.insertText`
  typing per-char humanized delay. RL shapes (rl_mouse_v6.npz) loaded.
- Engine ladder me `engine='wire'` mode: `GhostSession(engine="wire")`
  — apna protocol + apna input. (auto mode me CloakBrowser pehle
  rehta hai — battle-tested; wire explicit.)

LIVE-TESTED (03-Sep-2026): goto/evaluate/text ✓, httpbin 200 ✓,
WireMouse move/click/type/scroll ✓, GhostSession drop-in ✓
(title/evaluate/human interface), stealth init
(navigator.webdriver=None, plugins.length=5) ✓.

CF managed-Turnstile honest note: wire pe bhi token mint silent-fail
— root cause chromium-BUILD detection hai (raw protocol se bachta
nahi). Wire ka apna value: protocol+input fingerprint hamara, koi
third-party runtime dependency nahi, aur non-CF walls ke liye sabse
clean surface.
