# GhostRise 🌙

Anti-detect browsing engine for ProxyRise — headed **and** headless with
identical fingerprints, native proxy rotation, persistent identities.

> Spec: don't just pass challenges — be unremarkable. No signal should
> exist to detect.

## Engine: CloakBrowser (source-level stealth)

v0.2 rides **CloakBrowser**: a Chromium build with 49 source-level C++
patches — canvas, WebGL, fonts, GPU, WebRTC, network timing, automation
signals, CDP input behavior. Nothing is injected via JavaScript; the
stealth is compiled into the binary.

Verified against live detection stacks:
- Cloudflare Turnstile: PASS (auto-resolve + managed)
- reCAPTCHA v3 score: 0.9 (human-class)
- FingerprintJS / BrowserScan: clean

Why this beats runtime patching (playwright-stealth etc.): detection
stacks read the TLS handshake and engine internals that JS patches can
never reach. Here every layer ships inside the browser itself.

## Usage

```bash
pip install cloakbrowser          # host dep - binary auto-downloads

# identity vault - stable persona across sessions
python3 -m ghostrise.cli create work1 --os windows --locale en-US

# headless through a ProxyRise exit
python3 -m ghostrise.cli open https://target.com -p work1 \
    --proxy user:pass@host:port --shot page.png

# headed - same fingerprint, visible window
python3 -m ghostrise.cli open https://target.com -p work1 --headed
```

Auto-integrations:
- `~/.solver_clearance/<domain>.json` cookies replayed automatically
- WebRTC exit-IP spoofing + geoip timezone/locale matching (engine-side)
- `humanize=True`: Bezier mouse curves, per-char typing, scroll physics

## Human behavior layer (for AI agents)

Agents driving the browser get human-shaped primitives:

```python
with GhostSession(profile="agent1", proxy=proxy) as ghost:
    page = ghost.page("https://target.com")
    human = ghost.human(page)
    human.type("input#search", "residential proxies", submit=True)
    human.dwell()
    human.scroll(800)
    human.click("a.results > article:first-child")
```

Bezier mouse travel with overshoot correction, lognormal keystroke
cadence with rare typo+fix, eased chunked scrolling, reading dwells -
the behavioral tells that flag instant/linear agent actions are gone.

## Server mode (VPS / Docker)

```bash
docker run -d --name ghost -p 127.0.0.1:9222:9222 cloakhq/cloakbrowser cloakserve
# then connect from anywhere via connect_over_cdp("http://host:9222")
```

## Python API

```python
from ghostrise import GhostSession

with GhostSession(profile="work1", proxy="user:pass@host:port") as ghost:
    page = ghost.page("https://target.com")
    print(page.title())
```

## Roadmap to a fully self-built engine

| Phase | Goal | Notes |
|---|---|---|
| 0.1 | Anti-detect control layer | done |
| 0.2 | CloakBrowser engine swap | this release |
| 0.3 | Pool rotation built-in | round-robin + health scoring via recon/pool_score |
| 0.4 | Deterministic fingerprint gen | seed -> full fp tree per identity |
| 1.0 | Own Chromium fork | fold the whole stack into ProxyRise's binary; JA3-level control |

Phase 1.0 is where "0% captcha rate" becomes an engineering guarantee:
every observable layer owned by us.
