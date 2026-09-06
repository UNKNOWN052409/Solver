# Browser / Solver restructure — migration plan (NO solver breakage)

Goal: put browser + solver in ONE repo, SEPARATE folders, where the browser is
rust/lightweight and the solver is untouched. This repo already has the browser
code in two legacy places and the solver depends on one of them.

## The hard constraint

`solver/vision/harvest.py` does `from ghostrise.engine import GhostSession`, and
`solver/vision/solve_with_fallback.py` references `ghostrise/ai_assistant`.
So **physically moving `ghostrise/` out of the package root breaks `solver/`.**

Therefore this pass does a **label + plan** migration, not a physical move, so
`solver/` runs exactly as before. The browser's own Rust workspace is genuinely
created fresh under `browser/` and is fully independent of the Python package.

## What was created in this pass

| Action | Path |
|---|---|
| New design doc | `docs/BROWSER_DESIGN.md` |
| New browser workspace | `browser/Cargo.toml`, `browser/src/main.rs` |
| New label/map doc | `browser/README.md` |
| Built+verified binary | `browser/target/{debug,release}/comet` |
| **solver/** | **untouched (verified — see below)** |

## Target layout (the destination after the planned phased migration)

```
NeoSolver/
├── browser/                    # rust browser (COMET)
│   ├── comet/                  # main bin crate (now: src/main.rs)
│   ├── core/                   # lightweight HTTP core from ghostrise/engine,wire,ac_browser
│   ├── compiler/               # inbuilt compiler from ghostrise/compiler,browser_agent
│   ├── mcp/                    # MCP server from ghostrise/mcp_vision,search_mcp
│   ├── solverapi/              # anti-captcha solver API consumer (calls ../solver)
│   └── proxy/                  # same-IP + residential from ghostrise/identity,pool,behavior
├── solver/                     # UNTOUCHED — serves /solve to browser/solverapi
└── docs/BROWSER_DESIGN.md
```

## Migration steps (rollout, not done yet to avoid breaking solver)

1. **Port the Rust core first.** Keep `../src/main.rs` as the reference; split its
   `mod` blocks into `browser/core`, `browser/mcp`, `browser/proxy` crates. No
   Python touched. Solver unaffected.
2. **Only after the Rust port is feature-parity** (open/run/solve/serve against
   real sites), move `ghostrise/` under `browser/legacy-python/` and **re-point the
   one solver import**: `solver/vision/harvest.py` →
   `from browser.legacy_python.ghostrise.engine import GhostSession` (and same for
   `solve_with_fallback.py`). This is the single solver edit, and it is delayed
   until the Rust port replaces the dependency.
3. **Verify** `python -c "from ghostrise.engine import GhostSession"` still resolves
   during the interim, and that the solver's HTTP serve + `solver/vision` import
   chain is green.

## Verify solver was NOT touched

```bash
git -C /home/kali/NeoSolver status -- solver/      # should list no modifications
git -C /home/kali/NeoSolver diff --stat -- solver/ # empty
```
