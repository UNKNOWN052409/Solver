# GhostEngine RE Self-Test Report (dogfooding — apna toolkit, apna product)

**Date:** 04-Sep-2026 · **Method:** apna revkit (MITM browser-map + binary RE) apne hi
GhostMouse/GhostEngine stack pe chalaya. Dogfood verdict: RE-toolkit working,
product ki RE-hardening gaps documented.

## A) Binary RE — stripped release binary, kya leak hua

Target: `target/release/ghostmouse` (ELF aarch64 PIE, stripped, BuildID aad66f54)

| RE technique | Kya nikala |
|---|---|
| `strings` + filter | Poora CLI surface: 16 commands + flags (get/links/form/sniff/x/x-search/solve/battery/serve/whoami, --persona/--proxy/--api/--min-score) |
| API contract | X-API-Key, X-2Captcha-Key, /solve/image, /solve/service, TWOCAPTCHA_KEY env |
| X fallback chain | syndication.twimg.com/tweet-result, __NEXT_DATA__ parse, rss.xcancel.com, nitter.net/poast/tie instances |
| Persona DB | win-chrome-136/135/134, mac-chrome-136/135, lin-chrome-134 TLS profiles |
| Captcha detectors | recaptcha/turnstile/hcaptcha/geetest/arkose/datadome/signatures sab readable |
| Tracker blocklist | 60+ ad/tracker domains (doubleclick→outbrain) |
| Dep-stack leak | html5ever-0.27, tokio-1.53.1, cargo registry paths |

**Verdict:** stripped binary me bhi business logic full exposure —
countermeasure backlog: literal obfuscation / crypto-strings (M-later).
Ye exactly wahi attack-surface hai jo revkit clients ke against use hota hai.

## B) revkit browser-MITM — apna HTML demo

`revkit endpoints` (Netflix capture se pehle) + engine-anti selftest:
- anti::detect selftest HTML pe: `recaptcha-v2 sitekey=SELFTEST123 keyless=true` ✓
- forms/links extraction ✓ — engine ka structured surface RE-friendly (by design)

## C) Engine symbol surface

`.rlib` symbols stripped-safe (metadata-only); binary side hi exposure-surface hai.

## Engine M-status after this session

- M1 parser/DOM + human/anti layers — merged (42/42 tests)
- M2 net-session (cookies/redirects/auth/Opera-RE-doc) — merged
- M3 form-submit + OAuth + API-keys + AES-GCM vault — merged
- M4a js interpreter — agent-retry live (js-core2)
- Pending queue: js-web, css-render, DRL, captcha-auto, qwen-connect, vision-MCP, integrate
