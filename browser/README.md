# Comet — `browser/` workspace (browser half of the one-repo / two-folder model)

> LO directive: **browser and solver in ONE repo, SEPARATE folders.** This folder is
> the **browser half**. The solver half lives in `../solver/` and is **untouched** by
> this workspace.

**The one invariant:** `browser/` is an independent Rust workspace. It never
moves, edits, or imports any `solver/` file. Its only coupling to the solver is a
**network call** from `browser/solverapi/` to the solver's HTTP endpoint
(default `http://127.0.0.1:8081/solve`, configurable via `COMET_SOLVER_URL`).

## What is here (real, built & verified)

| Path | What | Verified |
|---|---|---|
| `Cargo.toml` | Comet package, low-resource release profile (`opt-level=z`, `lto`, `strip`, `panic=abort`) | `cargo build` exit 0 |
| `src/main.rs` | std-only Comet core | inbuilt compiler + server verified |
| `target/release/comet` | built binary (398 KB) | `comet run` + `comet serve` tested |

## Subcommands (all real)

| Command | Action |
|---|---|
| `comet run <lang\|path>` | Inbuilt compiler: detect lang → spawn toolchain → stream output |
| `comet serve [--port N]` | HTTP/MCP-ready server (`/health`, `/tools`) |
| `comet open <url>` | HTTP GET via std TcpStream |
| `comet whoami` / `comet config` | persona + proxy/solver env state |
| `comet solve <sitekey> [url]` | prints the solver-api request contract |

## Mapping of legacy browser code → this `browser/` folder

The existing browser code lives in two places: the Python stack `../ghostrise/`
and the old single-file Rust core `../src/main.rs`. Neither is moved in this
pass (see [MIGRATION_PLAN.md](MIGRATION_PLAN.md) for why — `solver/` depends on
`ghostrise`, so a physical move would break solver). The target homes are:

- `../src/main.rs` (old GhostMouse core) → `browser/comet/src/` (the `core/`,
  `mcp/`, `proxy/` crates).
- `../ghostrise/engine.py, wire.py, ac_browser.py` → `browser/core/`
- `../ghostrise/identity.py, pool.py, behavior.py` → `browser/proxy/` + `core/identity`
- `../ghostrise/compiler.py, browser_agent.py` → `browser/compiler/`
- `../ghostrise/mcp_vision.py, search_mcp.py` → `browser/mcp/`
- `../ghostrise/captcha_agent.py, ac_browser.py` → `browser/solverapi/`

## Anti-captcha solver API folder (the browser's consumer)

`browser/solverapi/` is planned as the place where the browser detects a captcha
and **delegates** to `../solver/` over HTTP — it does not solve captchas itself.
Request body it POSTs (see `comet solve`):
```json
POST /solve
{ "sitekey": "0x4AAA...", "type": "recaptcha_v2",
  "url": "https://example.com/login", "image_b64": null }
```
The solver's response token / grid coords are stored in the clearance vault and
replayed. Full contract: `docs/BROWSER_DESIGN.md` §4.6.

## Build

```bash
cd browser
cargo build --release   # target/release/comet  (std-only, no network needed)
./target/release/comet  run  path/to/code.rs
./target/release/comet  serve --port 8765      # MCP-ready server
```
