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

### F1. Image captcha SOLVE accuracy — BROKEN (0/3 correct)
- What: `solver.cli solve` runs but wrong: true `4m4xj`→`anpxy`, `q1q0e`→`qlqe`, `yvps0`→`yis0`.
- Why: **no trained CNN/TileNet model** (no .pt/.onnx file) **and torch is not installed** in this env. The "ensemble" falls back to weak heuristic OCR with no learned weights.
- Fix: install torch + train TileNet on harvested/hand labels serving `solver/vision/serve.py` `/classify` + `/rotate`. Then the keyless grid path in `captcha_agent._hcaptcha_grid_solve` gets a real brain.

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

## Solve pipeline (added — D1 redo, verified)
`solver/vision/solve_with_fallback.py`: confidence-guarded 3-stage chain —
  local ensemble (conf from % variant agreement) -> vision-LLM
  (VISION_LLM_URL/MODEL, base64 PNG, "transcribe exactly") -> honest
  `heuristic-lowconf` flag. No false "solved": live local exact=1/10, so
  heuristic conf is hard-capped 0.65 and defaults to lowconf until a vision
  backend actually returns. Verified: vision chain returns method='vision'
  conf=1.0 when a vision endpoint answers (mock endpoint test PASS).
  CLI: `solve <img> --fallback` prints text+confidence+method.

## Priority
1. Point VISION_LLM_URL at a real VLM (qwen2.5vl/llava) -> F1+F2 solved live
2. Torch + TileNet train (removes the vision network dependency)
3. ffmpeg + STT audio (F3)
4. proxy pool feed (F4)
5. verify on live site, not demo
