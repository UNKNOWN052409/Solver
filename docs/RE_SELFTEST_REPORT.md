# GhostEngine RE Self-Test Report (dogfooding — apna toolkit, apna product)

**Date:** 04-Sep-2026 · **Commit:** 26283f0+ · **Session:** qwen (20260901_025500_6144a5)

## Method
Apna revkit (MITM browser-map + binary RE) + apna hi engine-code apne products pe:
(a) external view — revkit browser-MITM se Rust binary + Python engine traffic-map,
(b) internal view — engine anti-detect module apne hi demo HTML pe,
(c) offline dogfood — mock hcaptcha challenge pe poora grid-solve chain.

## Results — ALL ARMS COMPLETE

### Arm 1: Binary RE (ghostmouse, stripped release)
- CLI surface fully reconstructed from strings: 16 commands + flags
- API contract leaked: X-API-Key/X-2Captcha-Key, /solve/image, /solve/service
- X-chain internals: syndication.twimg.com, __NEXT_DATA__, rss.xcancel.com, nitter fallbacks
- Persona DB: win/mac/lin-chrome-134..136 TLS profiles
- Tracker blacklist (60+ domains) + captcha detector signatures readable
- Build-stack leak: html5ever-0.27 + tokio-1.53 + cargo paths

### Arm 2: Engine anti-detect self-test (Rust example)
`cargo run --example anti_selftest /tmp/anti_demo.html`:
- turnstile DOUBLE-detect (script-src + hidden cf-turnstile-response)
- walled=true, title/forms/meta-2fa extract — LIVE GREEN

### Arm 3: revkit MITM dogfood (Python)
5/5 OK: binary-strings, binary-symbols, engine-lib, revkit-endpoints, solver-API probe.

### Arm 4: hCaptcha grid-solve OFFLINE dogfood (post-fix)
Mock challenge DOM + REAL harvested tiles + live vision-serve:
GRID-SOLVED=True, prompt='a bus', clicked=9.
Chain: prompt-extract → tile-b64 → TileNet classify → prompt-class match → humanized clicks → verify.

## Bugs found & fixed BY the self-test (dogfood ka asli proof)
1. **M1 tokenizer self-closing bug** — `<meta .../>` ke baad next tag ka `>` swallow
   (form-extraction empty). Fix + regression test (64/64 green).
2. **vision labels format-mismatch** — serve list-of-lists deta tha, agent string-compare
   karta tha — real-site pe silently fail hota. Fix: flat-normalize.
3. **wire nth(i) semantics** — :nth-of-type galat semantics tha, querySelectorAll[i] fix.
4. **bbox key-format** — JS w/h vs playwright width/height mismatch.
5. **HumanActions.move_to** — string-only target, object-handles unsupported. Fixed.

## Live-site status (honest)
- dashboard.hcaptcha.com/demo: ab LOGIN-WALLED (unauthenticated access khatam)
- hcaptcha.com homepage widget: wire-FP pe "Please try again" session-trust state
  (engine-compare: wire reject vs cloak lazy-mount — fingerprint-gate class, documented)
- recaptcha-v2 2captcha demo: LIVE — anchor-click + bframe + 9 tiles chain working,
  500-scale harvest in-progress (naye grids: fire hydrant/cars/crosswalks)

## Verdict
RE-selftest POORA PASS — 4 arms, 5 real bugs pakde aur fix hue, sab kuch push pe
(9ffb1cc → 5f79f11 → 26283f0). Engine apne hi tools se tested + hardened.
