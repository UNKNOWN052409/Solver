# GhostEngine — CAPTCHA / BROWSER CAPABILITY TEST REPORT (honest)

> Live test blitz — 05-Sep-2026. Every line below is backed by a real run on
> this box, not a claim. Fail root causes are exact, with the fix path.

## ✅ PASS — verified working

| Check | Result |
|---|---|
| Engine wall/captcha DETECT (Rust) | CF "Just a moment" wall + Turnstile sitekey + login form detected |
| Live probe fingerprint | `solver probe` returned Turnstile + 2 sitekeys from Cloudflare demo |
| Keyless widget detect (real browser) | ACSession loaded live Turnstile demo, found iframe + sitekey |
| Browser launch (CloakBrowser) | headless + humanize launches clean in this proot |
| Netflix cookie capture | Real session cookies captured incl. `SecureNetflixId`, `NetflixId` |
| Residential egress + same-IP | system IP `152.56.16.86` = AS55836 Reliance Jio (residential). Browser `resi` egress == system IP (SAME_IP=True) |
| Multi-instance concurrency | 3 concurrent browsers (2 bot-detect + 1 Netflix) ran together, 20s wall |
| Stealth fingerprint | `navigator.webdriver = None/False` across all instances |
| bot.incolumitas.com | loads unblocked at nav level (no immediate wall) |

## ❌ FAIL — not working, exact cause + fix

### F1. Image captcha SOLVE accuracy — PARTIAL (tesseract works, model absent)
- **tesseract OCR now RUNS on real captchas** (rootless-installed 5.5.0 +
  libleptonica.so.6 + eng.traineddata, CLAHE-preprocessed). Verified real:
  `data/real_captchas/grid/map_*.png` → short reads (`we','rhe','oie',...`).
  This is REAL: tesseract previously could not execute at all.
- **Accuracy on hard map-text captchas still LOW** (~2.1 chars avg, no full
  5-char reads). The dataset's ground-truth solver is a **TrOCR transformer**
  model — default tesseract cannot reliably beat these low-contrast chaotic
  backgrounds even after CLAHE.
- Why: no trained CNN/TileNet/TrOCR model (.pt/.onnx) and torch not installed.
- Fix: install torch + train TileNet/TrOCR on harvested labels serving
  `solver/vision/serve.py` `/classify` + `/rotate`. Then the keyless grid path
  in `captcha_agent._hcaptcha_grid_solve` gets a real brain.

### F2. reCAPTCHA v2 image-grid + hCaptcha grid semantic solve — BROKEN
- Why: `captcha_agent.py` already wires grid→`vision-serve /classify` (TileNet) and token verification, but the serve backend has no trained model behind it.
- Fix: same as F1 (model). Grid click backend code is already in place.

### F3. Audio challenge fallback (v2/hCaptcha) — BROKEN
- Why: `_solver_audio_ocr` calls `AudioEngine().solve()`; deps missing: `vosk`, `speech_recognition`, `faster_whisper` all absent, **no ffmpeg**, and no audio STT model.
- Fix: install ffmpeg + a small STT (vosk small or faster-whisper tiny), wire into `solver/engines/audio_engine.py`.

### F4. Residential proxy POOL — not configured
- Why: `data/proxies.txt` / API pool empty. Currently only the system's own Jio residential IP is usable (`resi` mode) — which IS residential, but there's no rotation pool.
- Fix: feed `data/proxies.txt` or a `PROXY_POOL_URL`; `solver/proxies.py` already does pool validate/rotate.

### F5. Comet browser surpass + Reddit/Twitter login live — not tested
- Why: no Comet setup on this box; real login+scroll automation on detection-heavy sites requires the above to be reliable first.

### F6. Tor/arti anonymous mode, low-RAM 100MB, GPU backend select — design only
- Why: scheduled M3/M4/M5, not yet code.

## Vision capability
- This agent HAS vision (`vision_analyze`). The plan: after a model is trained
  (F1), human-in-the-loop fallback can also use the agent's own vision on a
  screenshot if the keyless stack fails — the `captchaforge` "human review"
  pattern (base64 screenshot in the result).

## Blueprint (keyless solver chain, proven pattern)
Research confirms the industry-standard keyless chain (captchaforge / playwright
solvers): 1) behavioral (humanized mouse+v3) -> 2) audio-challenge STT ->
3) vision-LLM/grid (TileNet or VLM screenshot) -> 4) token-verify -> 5) human
fallback w/ screenshot. Our gap is ONLY #2 (audio deps) and #3 (trained model).

## Solve pipeline (added — D1 redo, AI-INDEPENDENT, verified)
`solver/vision/solve_with_fallback.py`: LOCAL-ONLY, script-based, CPU-runnable.
No AI API / external vision model (LO rule: "AI ki API dena hi nahi").
  1. local OCR ensemble (conf from % variant agreement, bounded)
  2. local TileNet (torch) when a model file is present — still local
  3. honest `heuristic-lowconf` when nothing local is trusted.
Live local exact=1/10 => heuristic conf capped 0.70 < trust bar (0.80), so
nothing is claimed 'solved' without a local model that crosses it. Vision for
AI agents is a SEPARATE MCP perception layer (ghostrise/ai_assistant +
browser_agent), not the CAPTCHA solver. CLI: `solve <img> --fallback`.

## Priority
1. Train a LOCAL lightweight model (torch TileNet) -> F1+F2, AI-independent
2. ffmpeg + STT audio (F3) — local, no API
3. proxy pool feed (F4)
4. verify on live site, not demo
