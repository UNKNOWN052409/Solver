# GhostRise 🌙

Anti-detect browsing engine for ProxyRise — headed **and** headless with
identical fingerprints, native proxy rotation, persistent identities.

> Spec: don't just pass challenges — be unremarkable. No signal should
> exist to detect.

## Why engine-level (not runtime patches)

Runtime patching (playwright-stealth, partial patchright) only touches the
JS layer. Detection stacks read the TLS handshake (JA3/JA4), HTTP/2 frame
order, and rendering stack — none of which JS patches can reach.
GhostRise v0.1 rides **Camoufox** (Firefox fork, spoofing at C++ level):
one binary, one rendering path, headed or headless — no differential
signal between modes.

## Usage

```bash
pip install 'camoufox[geoip]' && python3 -m camoufox fetch   # host deps

# identity vault - stable persona across sessions
python3 -m ghostrise.cli create work1 --os windows --locale en-US

# headless (default) through a ProxyRise exit
python3 -m ghostrise.cli open https://target.com -p work1 \
    --proxy user:pass@host:port --shot page.png

# headed - same fingerprint, just visible
python3 -m ghostrise.cli open https://target.com -p work1 --headed
```

Auto-integrations:
- `~/.solver_clearance/<domain>.json` cookies replayed automatically
- geoip matching keeps locale/timezone consistent with exit IP
- WebRTC blocked at engine level (no real-IP leaks)

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
| 0.1 | Camoufox control layer | this release |
| 0.2 | Pool rotation built-in | round-robin + health scoring via recon/pool_score |
| 0.3 | Deterministic fingerprint gen | seed -> full fp tree, not just config subset |
| 0.5 | Own Firefox fork | rename strings, strip Mozilla tells, ship branded builds |
| 1.0 | Own Chromium fork | BoringSSL-level JA3 control; the long game |

Phase 1.0 is where "0% captcha rate" becomes an engineering guarantee:
every observable layer owned by us.
