# GhostWire Browser — Features Audit (LO 2026-09-06)

Honest inventory of the browser stack (`ghostrise/`, 22 modules) vs LO's
requested feature set. Status = IMPLEMENTED / PARTIAL / MISSING / BROKEN,
determined by reading each module (real code, self-tests noted).

## Legend
- **IMPLEMENTED** — real logic present, verified by code/selftest
- **PARTIAL** — works but incomplete / not end-to-end verified
- **MISSING** — no code / only a reference or stub
- **BROKEN** — code exists but currently fails (blocked dep / runtime)

## Feature x Status

| Feature (LO ask) | Status | Where | Notes |
|---|---|---|---|
| Same-IP enforcement | IMPLEMENTED | capture_browser.check_same_ip | browser egress == system IP (152.56.16.86), proxy='resi' |
| Same-IP POOL (multi-browser) | PARTIAL | capture_browser, orchestrator | single-browser verified; N-browser same-IP pool not fully exercised |
| Anon / Tor-grade stealth | PARTIAL | wire.py GhostWire, ac_browser | webdriver=False, fingerprints; full Tor-like circuit rotation MISSING |
| Multi-layer password vault | IMPLEMENTED | engine/ src/vault.rs, crypt.rs | Rust AES-GCM+ChaCha20+Argon2id; 76+3 tests pass |
| MITM cookie/endpoint capture | IMPLEMENTED | capture_browser.py | 9 real Netflix cookies incl SecureNetflixId captured; CDP |
| GPU / 4K rendering | PARTIAL | runtime.py, cli.py, orchestrator | GPU detect + resolve exists; 4K viewport not wired |
| Browser compiler | PARTIAL | browser_agent.py | tool-verifier; full compile-serve pipeline MISSING |
| AI assistant (form-fill/nav) | IMPLEMENTED | ai_assistant.py, browser_agent.py | describe/fill/click verified real-browser |
| MCP vision for AI models | IMPLEMENTED | mcp_vision.py | real Chrome + example.com click verified |
| MCP search engine | IMPLEMENTED | search_mcp.py | web_search/extract/browse; MCP serve layer |
| OAuth / credential integration | IMPLEMENTED | oauth_integrations.py | CredentialStore + OAuthFlow + captcha-service; selftest PASS |
| Drive-backed browser storage | IMPLEMENTED | drive_browser.py | profiles/downloads/assets on Drive; selftest PASS |
| Humanized mouse (bezier/RL) | IMPLEMENTED | wire_mouse.py, behavior.py, rl_mouse.py, click.rs | real CDP click verified; RL traj present |
| Master orchestrator + agents | IMPLEMENTED | orchestrator.py | real hermes workers; selftest PASS (inline+worker) |
| Same-IP pool request: LO | PARTIAL | (as above) | remaining explicit gap |

## Per-Module One-Liner

- **ac_browser.py** — ACSession: anti-Captcha-frame session (CF/Turnstile), sitekey decode.
- **ai_assistant.py** — AiAssistant + _Compat wrappers: page describe / extract / fill / click.
- **behavior.py** — HumanActions: bezier + gesture timing.
- **browser_agent.py** — AgentLoop: instruction->tool->action; compiler tool-verify.
- **captcha_agent.py** — reCAPTCHA/hCaptcha solve; audio fallback (vosk now wired); grid click.
- **capture_browser.py** — CaptureBrowser: same-IP enforce + vault login + MITM capture.
- **cli.py** — GhostWire CLI (create/list/delete/open sessions).
- **drive_browser.py** — BrowserProfileStore/DownloadRedirect/HeavyAssetStore (Drive).
- **engine.py** — GhostSession: proxy + vault cookies + clearance replay.
- **mcp_vision.py** — PerceptionLayer + VisionModelSupport: DOM map + click, HTTP MCP.
- **oauth_integrations.py** — CredentialStore (Fernet master) + OAuthFlow + captcha-service auth.
- **orchestrator.py** — TaskQueue + ResourceManager + AgentSpawner + MasterOrchestrator.
- **profiles.py** — browser profile create/load/list.
- **rl_mouse.py** — TrajectoryEnv/PolicyMLP: RL mouse trajectory training.
- **runtime.py** — mem/CPU/reverse-tier detect (resource awareness).
- **search_mcp.py** — web_search/extract/browse MCP harness.
- **store.py** — Drive upload/download (copyto fixed) + retry-backoff.
- **wire.py** — GhostWire: CDP browser launch + WS (launch flake in proot noted).
- **wire_mouse.py** — WireMouse: CDP Input.dispatchMouseEvent human mouse.
- **x_agent.py** — X/Twitter search agent.

## TOP-5 most-impactful gaps (block LO's full vision)

1. **Same-IP multi-browser POOL** — many browsers, one enforced IP per task.
   Touch: `capture_browser.py` + `orchestrator.py` (pool manager).
2. **Tor-grade per-context circuit rotation** — new identity per context.
   Touch: `wire.py` (proxy chain per GhostWire instance).
3. **Browser compiler pipeline** — write->compile->run serverside.
   Touch: `browser_agent.py` (add compile+exec stage).
4. **4K / high-DPI viewport + GPU offload to browser**.
   Touch: `wire.py` (launch flags) + `runtime.py` (GPU->viewport).
5. **reCAPTCHA/hCaptcha live end-to-end** — needs trained TileNet (torch now
   installed) + real grid data; tiler.py landed. Touch: `captcha_agent.py`.

## Honest bottom line
Core bones are real and verified (same-IP, vault, MITM, Drive, MCP, human
mouse, orchestrator). Not yet full LO vision: same-IP *pool*, Tor-grade
anonymity rotation, real compiler, 4K GPU, and live-reCAPTCHA solve (needs
torch+model, now unblocked since torch 2.14 works).
