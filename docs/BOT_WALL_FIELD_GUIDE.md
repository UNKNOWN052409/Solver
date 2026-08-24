# Bot Wall Field Guide 🥷

How JS bot-walls detect you, and how to walk past them.
Written for AI-agent operators and proxy infrastructure teams.

---

## 1. The Five Detection Layers

Every bot wall — Cloudflare, Vercel Checkpoint, Akamai, DataDome,
PerimeterX — scores you across five layers. You only need to FAIL ONE
to be blocked, but must pass ALL to be invisible.

| # | Layer | What it reads | Weak spot of patched tools |
|---|-------|---------------|---------------------------|
| 1 | **IP reputation** | ASN type (datacenter/resi/mobile), abuse history, geo consistency | Datacenter IPs die regardless of everything else |
| 2 | **Engine fingerprints** | canvas/WebGL/audio/fonts/GPU/hardware via JS reads | JS-injection patches leave timing + consistency tells |
| 3 | **Protocol identity** | TLS JA3/JA4, HTTP/2 frame order, header order | Python/Go HTTP clients scream even with perfect headers |
| 4 | **Automation signals** | `navigator.webdriver`, CDP `Runtime.enable` leaks, `HeadlessChrome` UA, missing chrome-object bits | Playwright/Puppeteer leak CDP usage itself |
| 5 | **Behavior** | mouse entropy, typing cadence, scroll physics, dwell time | Straight-line mouse = instant flag |

## 2. Wall Species (know your enemy)

### Cloudflare managed challenge ("Just a moment...")
- JS proof-of-work + fingerprint collection, then clearance cookie
- Clearance is **bound to IP + UA**: replay only from same exit
- No sitekey -> token farms must browse THROUGH your proxy

### Vercel Security Checkpoint (429)
- Same species: edge PoW challenge, no captcha widget
- Holds simple clients hard; needs full stealth-browser JS execution
- Often auto-clears for genuine fingerprints after one reload cycle

### reCAPTCHA v3 / hCaptcha passive scoring
- No visible challenge; scores your whole session 0.0-1.0
- Beaten by behavior + reputation, not by clicking anything
- v3 score 0.9+ requires engine-level stealth (see CloakBrowser tests)

### Classic image captchas
- Legacy; trivially automated via OCR/CNN or solving APIs
- See Solver's engines - this repo's original mission

## 3. The Bypass Ladder (cheapest first)

```
L1  Right exit IP          resi/mobile > datacenter; match geo everywhere
L2  Engine-level stealth   CloakBrowser/Camoufox class - C++ compiled, not injected
L3  Leak-free control      no CDP Runtime.enable; connect_over_cdp only where safe
L4  Behavior               humanize=True class input shaping
L5  Session economics      mint clearance ONCE, vault, replay until expiry
L6  Delegation             solving services for the stubborn remainder
```

Rule: never pay for L6 if L1-L5 already pass. Never blame L6 while L1
is broken.

## 4. Agent Recipes (why agents die, and the fix)

AI agents (browser-use, Crawl4AI, Operator-class) fail differently than
scrapers: they live LONG, revisit sites OFTEN, and act PREDICTABLY.

### Recipe A - persistent identity per agent
One GhostRise profile per agent role. Fingerprint continuity builds
edge trust over days; fresh contexts reset trust to zero every run.

### Recipe B - clearance economy
Mint clearance headed once (`clearance_session.py mint`), share the
vault read-only with the agent fleet. Re-mint only on expiry/IP change.
Cost of a wall drops to ~zero per request.

### Recipe C - exit hygiene loop
`pool_score.py` weekly against top target domains -> retire burnt exits
before customers (or your agents) find them the hard way.

### Recipe D - behavioral floor
Always-on humanize for interactive flows. Deterministic instant actions
after page load are the #1 agent tell after IP reputation.

## 5. Self-audit checklist (run before blaming the wall)

- [ ] Exit IP residential/mobile and NOT previously abused?
- [ ] UA platform == actual OS arch == client hints?
- [ ] TLS fingerprint matches claimed browser? (test: tls.peet.ws)
- [ ] No `webdriver`, no `HeadlessChrome`, no CDP Runtime.enable?
- [ ] Canvas/WebGL/audio consistent across 3 reloads?
- [ ] Mouse paths have curvature + variable velocity?
- [ ] Clearance reused instead of re-challenged every request?

Score 7/7 before saying "the wall is unbeatable".

## 6. Tooling map (this repo)

| Need | Tool |
|---|---|
| Detect which wall species | `recon/cf_probe.py` (vendor-neutral) |
| Rank proxy pools per target | `recon/pool_score.py` |
| Mint/replay clearances | `recon/clearance_session.py` |
| Stealth browsing engine | `ghostrise/` (CloakBrowser) |
| Solve legacy captchas | `solver/` engines + API fallback |
| Delegated CF clearance | `solver.cli cf-clearance` |
