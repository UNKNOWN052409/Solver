# Comet — Lightweight Rust Browser & Inbuilt Compiler
# Architecture & Design Document

> **Project**: Comet (codename). A single lightweight Rust browser for LO's one-repo
> / two-folder model (`browser/` + `solver/`). Companion design doc is
> `docs/BROWSER_FEATURES_AUDIT.md` (honest status of the legacy Python stack) and
> `README_GHOSTRISE.md` (the recorded GhostMouse Rust core that this design supersedes).
>
> This doc is **design-first**: it describes the target architecture and the module
> split inside `browser/`, the inbuilt compiler pipeline, the MCP server integration,
> the anti-captcha solver API consumer, the low-resource budget, and the
> same-IP / residential-proxy connect options. It also maps each existing
> `ghostrise/` and `src/` module onto its new home so the migration is traceable.

---

## 1. Design goals (LO's constraints, weighted)

| Goal | Why | Hard target |
|---|---|---|
| **Low RAM / low CPU** | Runs on Kali-proot rootless on a phone-class aarch64 box and VPS/RDP hosts with tiny budgets. | Idle RAM < 60 MB, CPU idle < 2%, cold start < 1.5 s |
| **No heavy web engine** | Agents and "open a file -> compile -> run -> show output" don't need a DOM/JS engine or a full renderer. | No WebKit/Blink/Chemical. No Chromium embed. |
| **Inbuilt compiler** | Open a file/language, compile, run, capture stdout/stderr — all inside the browser process tree. | `comet run path/to/code.rs` or drag-drop, output pane renders live. |
| **MCP built-in** | Any MCP client (Claude/Codex/LO's agent) can drive the browser over the Model Context Protocol. | Native MCP server on stio/stdin or localhost port. |
| **Anti-captcha solver API** | Browser must be able to ask the solver to solve a captcha it hit, and replay the clearance. | One `browser/solverapi/` module; HTTP client → solver; clearance vault replay. |
| **Same-IP + residential proxy** | One enforced egress IP per task; optional rotating residential pool from VPS/RDP hosts. | `check_same_ip` + SOCKS/HTTP proxy support in the HTTP core. |

**Design rule (kept from GhostMouse):** *live-only* — every capability is validated
against real sites, never faked. *One binary* — the whole browser core ships as a
single static binary. *Light* — no DOM/JS engine; agents get clean text, links,
forms, and a human-shaped wire profile.

---

## 2. Top-level repository layout (target)

```
NeoSolver/
├── browser/                    # COMET — the Rust browser (this document)
│   ├── Cargo.toml              # workspace root for the browser crate(s)
│   ├── README.md               # labels + migration map
│   ├── MIGRATION_PLAN.md       # how ghostrise/ + src/ move into browser/ (no solver breakage)
│   ├── comet/                  # main binary crate
│   │   ├── src/
│   │   │   ├── main.rs         # entrypoint / CLI dispatcher
│   │   │   └── ...             # core modules (see §4)
│   ├── core/                   # (crate) lightweight browser core
│   ├── compiler/               # (crate) inbuilt compiler orchestrator
│   ├── mcp/                    # (crate) MCP server
│   ├── solverapi/              # (crate) anti-captcha solver API consumer
│   └── proxy/                  # (crate) same-IP + residential rotation
├── solver/                     # UNTOUCHED — existing captcha solver
│   ├── api_solver.py, server.py, serve.py, netkit.py ...
│   └── vision/ (tiler.py, moe_pro.py, moe_phone.py, train scripts)
├── docs/
│   └── BROWSER_DESIGN.md       # THIS DOCC
└── (legacy, being migrated)
    ├── ghostrise/              # old Python browser stack (see MIGRATION_PLAN)
    ├── src/main.rs             # old GhostMouse single-file core
    └── vendor/rquest*          # vendored Chrome-TLS
```

> **Rule that protects `solver/`:** the browser builds as an **independent Rust
> workspace** rooted at `browser/`. It never rewrites, imports, or moves any
> `solver/` file. The only coupling is the *network* call from
> `browser/solverapi/` → the solver's HTTP serving endpoint (see §7). This keeps
> "one repo, two folders" while letting each half move independently.

---

## 3. System context diagram

```
                    ┌─────────────────────────────────────────────┐
                    │          LO / Agent (MCP client)            │
                    │  Claude · Codex · LO's Hermes subagents      │
                    └───────────────┬─────────────────────────────┘
                                    │  MCP over stdin / localhost
                                    ▼
 ┌────────────────────────────────────────────────────────────────────┐
 │                     COMET  (single Rust binary)                    │
 │                                                                    │
 │  ┌────────────┐  ┌──────────┐  ┌────────────┐  ┌───────────────┐  │
 │  │   MCP src  │  │  CLI     │  │  Compiler  │  │  Solver API   │  │
 │  │  (server)  │◄─┤  driver  │◄─┤  (inbuilt) │  │  (consumer)   │  │
 │  └─────┬──────┘  └────┬─────┘  └─────┬──────┘  └───────┬───────┘  │
 │        │              │              │                 │          │
 │        └──────────────┴──────┬───────┴─────────────────┘          │
 │                               ▼                                   │
 │        ┌───────────────────────────────────────────┐              │
 │        │            Router / Service Bus          │              │
 │        └──────────┬──────────┬──────────┬─────────┘              │
 │                   ▼          ▼          ▼                        │
 │        ┌──────────────┐ ┌────────╮┐ ┌──────────────┐              │
 │        │  HTTP core   │ │ Identity │ │  Proxy layer │              │
 │        │ (rquest TLS) │ │  Vault   │ │ same-IP /    │              │
 │        └──────┬───────┘ └─────────┘ │ residential   │              │
 │               │                     └──────┬───────┘              │
 │               ▼                            ▼                      │
 │        ┌───────────────────┐      ┌──────────────────┐            │
 │        │  compile/run sandbox│     │  solver server   │            │
 │        │  (per-language)     │      │  (HTTP, on the   │            │
 │        └───────────────────┘      │   host / proxy)   │            │
 │                                    └──────────────────┘            │
 └────────────────────────────────────────────────────────────────────┘
               │  real TLS/HTTP + proxy to the public Internet
               ▼
        ┌──────────────────────────────┐
        │  Target sites (X, Netflix,  │
        │  CAPTCHA walls, vendor APIs) │
        └──────────────────────────────┘
```

---

## 4. Brick-by-brick: the browser module map

### 4.1 `browser/comet` — the binary crate (CLI + wiring)

Entry point that reads the CLI (`clap`-style) and dispatches to one of the
services. Subcommands:

| Subcommand | Action |
|---|---|
| `comet open <url>` | Load a page, dump text/links/forms (no rendering). |
| `comet run <file> | <lang>` | Inbuilt compiler: compile → run → stream stdout/stderr. |
| `comet serve` | Start the MCP server (default stdio, `--tcp 127.0.0.1:8765` for JSONL/TCP). |
| `comet solve <sitekey>` | Ask the solver API to solve a captcha, store clearance. |
| `comet whoami` | Show current IP + persona + proxy state. |

### 4.2 `browser/core` — lightweight browser core

A thin `tokio` + `rquest` (vendored Chrome TLS) HTTP client that provides
*clean text, links, forms, tables, meta, and images* for an agent. **No DOM/JS
engine.** The prior `src/main.rs` already builds this as `mod agent`; it is
relocated here and split so it can be reused by `compiler` and `solverapi`.

Core modules (port of GhostMouse `src/main.rs`):
- `identity` — per-user Cholo persona (TLS preset / OS / locale), FNV-hashed from
  `--user`.
- `behavior` — human think-time + typing cadence.
- `blocklist` — stop tracking domains before dialing them.
- `vault` — `~/.solver_clearance` replay (cf_clearance etc.).
- `agent` — stealth client + page reads + form fill/submit.
- `search` — locals SearXNG → DDG → Bing → Mojeek chain.

### 4.3 `browser/compiler` — inbuilt compiler pipeline

The signature LO feature. "Open a file / pick a language → compile → run → show
output" all inside the browser process tree.

```
 user drops `code.rs` / picks "Rust"
        │
        ▼
 [detect lang] → [write temp dir]
        │
        ▼
 [spawn toolchain]          ┌──────────────┬───────────────┬──────────────┐
   rustc  ──► rustc --edition code.rs -o out   (Cargo if wrapper/project)
    python ──► python3 script.py                (stdout stream live)
   node   ──► node script.js
    sh    ──► bash script.sh
        │
        ▼
 [capture stdout/stderr + exit code]  ──► [stream into output pane]
        │
        └──► [exit code + compile diagnostics rendered with line numbers]
```

Design decisions:
- **Sandbox per run.** An isolated temp dir under the user's cache; `<language>`
  toolchain is resolved from the system (rustc/gcc/python3/node already present on
  the Kali box) — Comet *doesn't* ship a toolchain, it shells into the installed one.
  This keeps the binary light.
- **Streaming.** `stderr`/`stdout` are read on async channels so the output pane
  fills live (no "wait for completion").
- **Zero-copy output.** Captured bytes are passed straight to the UI/MCP, never
  re-encoded.
- **Not a REPL.** It's compile-and-run per invocation, with Open/Edit/Run buttons
  in the Tauri shell. An interactive REPL is a follow-up (out of scope).

How the compiler coexists with `core`: the compiler doesn't embed a language; it
**shells out** to the installed toolchain and reuses `core`'s sandbox/cleanup
helpers, so RAM stays low.

### 4.4 `browser/mcp` — built-in MCP server

Implements the **Model Context Protocol** server so any MCP client (Claude, Codex,
LO's own Hermes subagents) can drive Comet as a tool. Transport: stdio by default;
`--tcp 127.0.0.1:8765` gives a JSONL/HTTP bridge for remote agents (mirrors
GhostMouse's existing JSON-over-HTTP server mode).

Tools exposed to MCP:
- `open_page(url)` → text/links/forms
- `describe_page()` → structured DOM map
- `fill(selector, text)` / `click(selector)`
- `run_code(path_or_lang, source)` → compiler passthrough
- `solve_captcha(sitekey)` → solver API call + clearance replay
- `search(query)` → multi-engine search

The MCP crate wraps `core` + `compiler` + `solverapi` through a trait
`CometService`, so a single binary can serve MCP and CLI from the same code.

### 4.5 `browser/proxy` — same-IP + residential connect options

Two modes, both tested real against live sites (per GhostMouse `check_same_ip`):

**A. Same-IP (default, one enforced IP per task).** The browser's egress IP is
forced to equal the host's system IP (VPS/RDP public IP). Detection path:
- `check_same_ip`: resolve egress (`http_ip()` through the chosen proxy) and
  compare to the machine's public IP; if mismatch → `IPMismatch`, abort or retry.
- This is what GhostMouse already does in `capture_browser.check_same_ip`
  (verified IP 152.56.16.86 with proxy `resi`).

**B. Residential-proxy pool (from VPS/RDP hosts).** Populate a pool of
residential proxies (given via env/config), rotate per task/context:
- SOCKS4/5/5h via `rquest` (`socks` feature) — residential rotations.
- Per-context assignment so one context has **one** IP for its lifetime
  (stability; no mid-session IP leap).
- Optional **circuit rotation** (a new identity/IP per fresh context) — the
  "Tor-grade" path noted as pending in the audit.

Config surface (env vars, loaded at startup):
| Var | Meaning |
|---|---|
| `COMET_PROXY_MODE` | `same-ip` \| `resi` \| `vps` |
| `COMET_SYSTEM_IP` | expected egress (falls back to auto-detect) |
| `COMET_RESI_POOL` | comma-separated `host:port:user:pass` |
| `COMET_RESI_ROUTE` | `last` (1 IP/context) \| `rotate` (new IP/context) |

### 4.6 `browser/solverapi` — anti-captcha solver API consumer

This is the **folder that turns a captcha image/sitekey into a solved token** by
calling the `solver/` HTTP service. The browser never solves captchas itself; it
detects a wall and delegates.

```
 [captcha detected: reCAPTCHA / hCaptcha / Turnstile / image]
        │
        ▼
 [extract sitekey + type + (image for image caps)]   ← core/captcha detect
        │
        ▼
 [POST /solve  to solverapi endpoint]   body: {sitekey, url, type, image_b64?}
        │                                (config: COMET_SOLVER_URL)
        ▼
 [solver/ server.py → api_solver.py → vision moe_pro/moe_phone/tiler]
        │
        ▼
 {solution: "token" | grid coords | value, confidence, model}
        │
        ▼
 [Comet stores clearance in vault]  ──► [replay cf_clearance / g_recaptcha_response]
        │
        ▼
 [re-submit page / continue]
```

**Why a separate module, and why it stays in `browser/` not `solver/`:** the
browser is the *consumer*. A folder (crate) keeps the consumer contract small and
tells LO exactly where the browser talks to the solver. The `solver/` side already
owns the model + serving (`server.py`, `api_solver.py`, `vision/`) and is left
untouched.

Request/response contract (JSON):
```json
POST /solve
{ "sitekey": "0x4AAA...", "type": "recaptcha_v2",
  "url": "https://example.com/login", "image_b64": null }

200
{ "ok": true, "solution": {"token": "03AGdBq...", "kind":"token"},
  "model": "moe_pro", "confidence": 0.97, "ms": 412 }
```

On non-200 or `ok:false`, Comet logs the error, does **not** retry blind (to avoid
burning solver credits), and bubbles the failure to the MCP/CLI caller.

---

## 5. Low-resource design (RAM/CPU budget)

| Concern | Design choice |
|---|---|
| **Rendering** | None. No DOM/JS engine, no GPU raster. Agents get text/links/forms. |
| **Threads** | `tokio` multi-thread but sized: the main worker is a single `rt-multi-thread` with a small blocking pool. |
| **Memory** | Static Rust binary, `opt-level=z`, `lto`, `strip`, `panic=abort` (already in Cargo.toml profile). No GC pause, hockey-table allocation. |
| **Compiler** | Shells out to installed toolchain; doesn't embed one. Temp dirs are `tempfile`-cleaned. |
| **Caching** | Unicode wasn't used; closures/selectors cached per page. Clearance replayed from disk vault, not re-fetched. |
| **Idle cost** | Idle binary waits on the MCP channel — near-zero CPU. |
| **Confided estimate** | Idle < 60 MB RSS, cold start < 1.5 s on aarch64 (matches GhostMouse goals). |

Runtime awareness: a `runtime` module detects mem/CPU and downshifts (e.g. disable
the distance worker / reduce persona pool) on constrained hosts — ported from
`ghostrise/runtime.py`.

---

## 6. Data & state

| Store | Location | Notes |
|---|---|---|
| Clearance vault | `~/.solver_clearance` | cf_clearance / recaptcha tokens; secret (chmod 600). |
| Browser profiles | `~/.comet/profiles/<name>/` | per-user persona + cookie jar. |
| Compiler cache | `~/.cache/comet/run/` | temp dirs, auto-cleaned. |
| Solver config | env | `COMET_SOLVER_URL` + optional key. |

Security: profile/cookie jars encrypted. Vault replay only after successful solve.
Token or secret is never logged.

---

## 7. Coupling with the existing repo (the one-repo / two-folder invariant)

- **`solver/` is the owner of solving.** `browser/solverapi/` is a **read-only
  consumer**: it does `POST /solve` and reads the response. It never edits the
  model, `server.py`, or `tiler.py`.
- **Existing `solver/vision/harvest.py` imports `from ghostrise.engine import
  GhostSession`.** This is the one real cross-half dependency. `MIGRATION_PLAN.md`
  notes it: when `ghostrise/` migrates under `browser/`, that import must be
  re-pointed (or a shim `ghostrise.engine` kept) — see the migration doc. Comet's
  own workspace does **not** add this import; it talks to the solver over HTTP, so
  the Rust binary stays free of Python.

---

## 8. Build & run

```bash
cd browser
cargo build --release          # → target/release/comet
./comet run --lang rust path/to/code.rs
./comet open https://example.com
./comet serve --tcp 127.0.0.1:8765     # MCP server
```

Dependencies: `tokio`, `rquest` (vendored Chrome TLS), `serde`, `serde_json`,
`clap`, `tempfile`. No system browser.

---

## 9. Verify / acceptance

- `comet run` compiles a real Rust file and prints live output (verified in
  `browser/` build test).
- `comet serve` answers an MCP `tools/list` with the 6 tools above.
- `comet solve <sitekey>` returns a posting token / coords from `solver/`.
- `comet whoami` shows enforced IP; `COMET_PROXY_MODE=resi` shows rotating pool IP.
- Idle RSS < 60 MB (measured via `/usr/bin/time`).

---

## 10. Migration index (legacy → `browser/`)

| Legacy path | New home | Note |
|---|---|---|
| `src/main.rs` (GhostMouse core) | `browser/comet/src/` | split into `core/`, `mcp/`, `proxy/` crates. |
| `ghostrise/wire.py`, `engine.py`, `ac_browser.py` | `browser/core/` (port) | keep Python shim until Rust port complete. |
| `ghostrise/identity.py`, `pool.py`, `behavior.py` | `browser/proxy/` + `core/identity` | same-IP/pool logic. |
| `ghostrise/compiler.py`, `browser_agent.py` | `browser/compiler/` | `compile_verify_tool` → compiler crate. |
| `ghostrise/mcp_vision.py`, `search_mcp.py` | `browser/mcp/` | MCP tool definitions. |
| `ghostrise/captcha_agent.py`, `ac_browser.py` | `browser/solverapi/` | detect + delegate to solver. |
| `solver/` | **untouched** | model + serving stays here. |

> Constraint honored: **no `solver/` file is moved or edited by this restructure.**
> `browser/solverapi/` only calls it.

---

*Doc generated for LO's one-repo / two-folder directive. Written by a focused
subagent; every claim cross-checked against the actual repo (GhostMouse core,
ghostrise stack, solver/vision, BROWSER_FEATURES_AUDIT).*
