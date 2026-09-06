# CAPTCHA LIVE TEST REPORT — 5sim.net + demo sites (real run)

> Date: 2026-09-06. Every endpoint hit, every site probed, and every solve
> below is a REAL execution on this box (Kali proot / aarch64). No mock, no
> fabricated counts. Harness: `tools/test_captcha_live.py`.

## 1. 5sim.net — REAL API contract (live-verified)

**Verdict (important): 5sim.net is NOT a captcha-solving service.** It is a
virtual-number / SMS-OTP rental API used *after* a captcha (captcha → phone
OTP). There is no `/createTask`, `/getTaskResult`, or token-solve endpoint.
The repo's existing `solver/fivesim.py` was already scoped this way; this run
confirms it against the live API.

Auth: `Authorization: Bearer <key>` in the header. The keyless guest endpoints
need no key; all user/buy endpoints return **401 without a Bearer key** (verified).

| Real endpoint | HTTP (this run) | Notes |
|---|---|---|
| `GET /v1/guest/countries` | 200 | keyless; returns full country map (153 countries) |
| `GET /v1/guest/prices?country=india` | 200 | keyless; `{country:{product:{operator:{cost,count}}}}` |
| `GET /v1/user/profile` | 401 | Bearer required → balance check |
| `GET /v1/store/buy-activation-number/{country}/{operator}/{product}` | 404 (this path/prod), otherwise Bearer | rent a virtual number (paid credit needed) |
| `GET /v1/user/check/{order_id}` | 401 | poll for the received SMS / OTP |

```jsonc
// buy (Bearer): GET /v1/store/buy-activation-number/india/any/google
{ "id": 12345, "phone": "+91xxxxxx", "country": "india", "operator": "any",
  "product": "google", "cost": 0.12, "status": "CANCELED" }
// check: GET /v1/user/check/{order_id}
{ "id": 12345, "status": "PENDING", "sms": [ { "text": "Your code is 861234" } ] }
```

**Paid/key requirement:** `FIVESIM_KEY` is not set in this environment and the
buy/check/buy-activation endpoints require a paid Bearer key + credit. The
**keyless** guest endpoints (countries/prices) are free and were used as the
reachable proof.

## 2. Real captcha demo sites — reachable + how to inject/submit

All four public demo pages are reachable (HTTP 200) from this host. Detection
and submit flow documented by the harness (regex scan of the live HTML):

| Demo | URL | Detected widgets | Status |
|---|---|---|---|
| reCAPTCHA v2 | https://www.google.com/recaptcha/api2/demo | recaptcha, turnstile | reachable |
| hCaptcha | https://accounts.hcaptcha.com/demo | recaptcha, hcaptcha, turnstile | reachable |
| Cloudflare Turnstile | https://turnstile-challenge-demo.globaldots-demo.cftenant.com/ | turnstile | reachable |
| 2captcha demo | https://2captcha.com/demo | recaptcha, hcaptcha, turnstile, geetest, funcaptcha | reachable |

**Inject / submit flow (universal):** the widget reads `data-sitekey`, mints a
client token, and the page's form POSTs that token (`g-recaptcha-response`,
`h-captcha-response`, `cf-turnstile-response`) with the form body to the site's
own server-side verify endpoint. Tokens are domain+IP-bound and validated
server-side. To "submit a challenge" you must obtain a valid token (not just
replay the page) — which is exactly where an image-grid/vision classifier is
needed for reCAPTCHA v2 / hCaptcha, and a real browser + residential IP for
Cloudflare Turnstile.

## 3. REAL solve + score (local, AI-independent solver)

Local solver = repo's own engine stack, driven through the userland tesseract
tree (`/tmp/tessroot`, tesseract 5.5.0, LSTM, whitelist-constrained). No AI API,
no network call.

**Real captcha data:** 40 real captcha images with REAL ground-truth labels,
fetched from `cavoixanh1806/captcha-map-solver` (`data/map_*.png` + the repo's
own `data/metadata.csv` giving the true `filename,text` answer). Verified with
vision that e.g. `map_00000.png` is a genuine text captcha ("4K1N9", label
`4KTN9`), not a mock — multicolor, overlapping, rotated characters on a noise
grid.

| Metric | Real result |
|---|---|
| total solved (exact) | **0 / 40** |
| failed | 40 |
| solve_rate | 0.0 |
| char accuracy | 7.5% |
| OCR engine | tesseract psm7 (uppercase whitelist) |
| solver errors | 0 |

Honest interpretation: off-the-shelf tesseract OCR does **not** crack these
real captchas (reads ~1–3 stray chars per image). The engine is functional —
it reads clean rendered text ("hello" on a blank canvas read correctly) — so
the failure is the captcha's distortion/overlap/color, not a broken OCR. This
**confirms F1/F2 of `CAPTCHA_CAPABILITY_REPORT.md`**: hard image captchas need a
trained local classifier (TileNet/TrOCR) — which is absent here (`torch` import
crashes on this aarch64 host, and there is no trained `.pt`/ONNX model wired
into the fallback path). A paid human-farm service (2captcha/capsolver) would
solve them, but no such API key is present in this environment.

## 4. What was NOT reachable / honest gaps

- 5sim captcha-solving: does not exist (it's phone numbers). Paid key absent.
- 2captcha.com (and any capsolver/anti-captcha) paid API key: **not set** — so
  the `solver/api_solver.py` external path was not exercised (no key).
- reCAPTCHA v2 / hCaptcha **image-grid** pass: not achievable with the local OCR
  stack (needs a vision classifier). Checkbox click is wired but a grid will
  usually follow; token generation for Turnstile needs a real browser +
  residential IP (datacenter/proot IP is refused by Cloudflare).
- `https://yitc.ddns.net:5100` (genuine-captcha public instance) was **down**.
- base64Captcha playground API and Eyevinn OSaaS captcha instance: **not
  exposed** (no public API endpoint reachable).

## 5. How to re-run

```bash
cd /home/kali/NeoSolver
export SOLVER_TESS_ROOT=/tmp/tessroot
python3 tools/test_captcha_live.py --sample 40 --out /tmp/captcha_test_result.json
```
