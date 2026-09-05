# GhostEngine — in-house stealth browser platform (replaces CloakBrowser)

> Architecture v1 — made by @torbug (for LO). This is the SYSTEM DESIGN that
> everything else is built against. Read this before writing any crate.

## 0. The mandate (why this exists)

We are **removing the CloakBrowser third-party dependency** entirely and
building our own enhanced stealth engine. Why:

- CloakBrowser is a closed-ish vendor dep; a build/fingerprint change there
  is out of our control, and it is the #1 foreign dependency in the stack.
- We need **Tor-grade anonymity, multi-layer encryption, GPU-scaled
  rendering, and full local control** — none of which we can guarantee while
  leaning on someone else's engine.
- Every feature below (password vault, cookie→MCP, same-IP guarantee, AI
  helpers, drive-backed storage) needs engine-level hooks that a vendored
  engine can't cleanly give us.

Result: **GhostEngine** — a Rust core that talks CDP/Wire to a hunkered-down
Chromium binary we pin, but with our OWN fingerprint layer, our OWN input
layer, our OWN crypto, our OWN routing. The Chromium *binary* is a
renderer/worker only; it has zero fingerprints of ours and zero knowledge of
our sessions. When we later ship a full self-renderer, the CDP layer stays
as a compat mode.

## 1. Layer map

```
┌───────────────────────────────────────────────────────────────┐
│  App / UI  (Tauri shell — Rust, or Electron later)            │
│   human UX + AI-driven control surface                        │
├───────────────────────────────────────────────────────────────┤
│  Orchestrator  (Rust)  — session lifecycle, provider routing, │
│   browser management, drive storage, MCP bridge               │
├───────────────────────────────────────────────────────────────┤
│  GhostEngine core (Rust)  — CDP/Wire, fingerprint layer,      │
│   input (humanized), routing (same-IP / Tor / pool), proxy    │
├───────────────────────────────────────────────────────────────┤
│  Crypt layer (Rust, RustCrypto) — password vault, cookie      │
│   vault, session encryption — multi-layer AEAD                │
├───────────────────────────────────────────────────────────────┤
│  Chromium-pinned binary (renderer/worker ONLY — no identity)  │
│   + GhostWire raw-CDP protocol                                │
└───────────────────────────────────────────────────────────────┘
       │                          │                     │
  GPU backend               Drive storage             Optional:
  (system / external /      (rclone-style,           VPN / API / Tor /
   low-RAM fallback)         keeps system disk free)  MCP search harness
```

Dependency rule: **GhostEngine core + crypt layer never import cloakbrowser.**
`ghostrise/*.py` keeps working as an app-layer facade but its `CloakBrowser`
path is deleted; it now binds to the Rust `ghostengine` pyo3 crate.

## 2. Removing CloakBrowser — concrete steps

1. Delete the `cloakbrowser` config path in `ghostrise/engine.py` (the
   `engine_pref in ("auto","cloak")` branch becomes the GhostEngine pyo3
   binding; `auto` = our engine first).
2. Keep GhostWire (raw CDP) as the always-available protocol layer.
3. Pin a Chromium binary we vendor (via `cargo`/a build script or a local
   copy) so we never depend on a third-party launcher.
4. `requirements.txt` drops nothing Python auto-imports; the Rust crate is
   built with `maturin`/`pyo3` and imported as `ghostengine`.
5. `--proxy resi` same-IP logic moves into the Rust router (below).

## 3. Anonymity + privacy (Tor-grade)

Two routing personalities, chosen per session:

- **RESI (default)** — route through the system's own egress. The browser
  exit IP **must equal** the server/system IP. If they ever differ the
  router hard-fails before a single byte leaks (this is the MITM/`server-to-
  server` detection guard: login "as the service" must never surface a
  foreign IP).
- **TOR** — optional onion circuit per session (embedded `arti` client, no
  external Tor binary). Browser fingerprint is generated fresh per circuit
  and is **never** reused across circuits — a Tor-ip leak can't trace back to
  a second identity.

Privacy guarantees:
- Every session uses an isolated, ephemeral profile.
- Canvas/WebGL/WebRTC disclosure is randomized per session (our own JS
  shims injected at engine layer, not a vendored lib).
- No telemetry, no phoning home. Local-only by default; network only when a
  VPN/API/Tor personality is explicitly chosen.

## 4. Encryption model (password + cookie vault)

Requirement hierarchy (who can read what):

| Role          | Can read passwords/cookies?              |
|---------------|------------------------------------------|
| User          | yes — with master password               |
| Service admin | yes — via ADMIN key (shown, never gen)   |
| Attacker/malware | NO, even with the file                 |

Design — **multi-layer cascade** (defense in depth, not one algorithm):

1. **Layer A — AEAD (AES-256-GCM)** per entry, key K_e.
2. **Layer B — ChaCha20-Poly1305** wrap of the whole blob (K_b), so even a
   successful AES break leaves a second wall.
3. **Layer C — Argon2id** key derivation (tuned high on GPU, safe-higher on
   CPU) guarded by **TPM/secure-enclave bound** when present.
4. **Layer D — access-context split**: user blob and admin blob share the
   ciphertext but are unlocked by DIFFERENT keys — user key (master pass),
   admin key (service key). Malware that steals the file gets ciphertext and
   neither key: the master pass is typed (never stored), the admin key lives
   behind the enclave/TPM, not in the file or in process memory.
5. **Failure mode**: wrong master pass → Layer A GCM tag fails → we refuse to
   even attempt B/C/D. Brute force on the file is bound by Layer C cost.
6. **KDF benchmark note**: per-grant rate-limit + memory-hardness means a
   stolen dump is ~impractical to crack locally; GPU accel lets *legit* un-
   locks be fast while *attack* parallelization is capped by per-file salt +
   iteration coupling.

Explicitly **no plaintext on disk, ever**. In-memory plaintext only during an
unlock window, dropped on lock/timeout.

**M5 note (dual-key admin read):** implemented M4 ships separate user-store
and admin-store paths (both multi-layer; each unlocks with its own key — test
`admin_access_path_independent` verifies the admin path end-to-end). The next
step is **dual-key single file** so a user-written store is openable by EITHER
the user master pass OR the service admin key — that's the "admin dekh sakta
hai, user dekh sakta hai, attacker nahi" model in one container.

## 5. Same-IP guarantee (the MITM/server-detection guard)

Mirrors / extends `ghostrise/capture_browser.check_same_ip` into the Rust
router:

```
browser_egress_ip == system_egress_ip ?  proceed
                                    :  hard-fail, no request leaves
```

Also compares the **TLS-JA3/JA4-style client fingerprint tuned locally** so a
remote side can't detect "browser talks like server but IP is foreign".
Detection surfaces covered: IPv4/IPv6 leak, WebRTC ICE leak, DNS-over-HTTPS
split brain, proxy env mismatch.

## 6. Password manager (built into the browser)

- Store per-site entries in the three-layer vault (site, user, pass, notes).
- Auto-fill login forms (humanized typing) from matched entries.
- Admin view: `--admin-key` unlock lists entries read-only.
- Cookie store: every logged-in provider keeps its cookies in a **cookie
  vault** (same encryption); these are the tokens that feed the provider
  checkers (see `Filtering/checker`) and the cookie→MCP converter.

## 7. Cookie → MCP conversion (Gmail / Notion / any logged-in)

A logged-in provider session exports:

- its cookies (encrypted vault),
- a generated MCP server config `mcp/<provider>/{email,db,drive,...}.json`
  that reads the cookie vault at runtime (never the raw secret on disk) and
  exposes `read/list/send` style tools against that service's API.

So one browser login powers many MCP integrations — no per-service MCP
install needed. Hermes/any MCP client can then drive it.

## 8. GPU + low-resource run modes

Explicit backend selection per session (Colab / external GPU / system GPU /
CPU-with-min-RAM):

- **GPU mode**: offload rasterize + the vision classifier (tile/object detect)
  to CUDA/Metal/WGPU. 4K rendering + S-scale sessions on an external GPU even
  when the laptop native res is 180px-ish.
- **Low-RAM mode**: Chromium launched with minimal heap, software raster,
  ~100 MB footprint; vision runs on a quantized (int8/onnx) model.
- **Least-resource-first**: the orchestrator auto-picks the smallest backend
  that meets the task, so "kam se kam resources pe zyada kaam".

## 9. Drive as storage (rclone-style, not system disk)

- All downloads / models / profiles / vault backups go to a mounted drive.
- `storage = Drive` config; orchestrator streams to/from drive, keeps the
  system disk nearly empty, and can **run from drive** via a shortcut.
- Same semantics as rclone: local path is a thin cache; source of truth is
  the drive.

## 10. Quan / model integration + AI helpers

- Connect one or more accounts (Quan, and any local/remote model endpoint).
- Natural-language browser ops at app level: "ye form fill kar de", "mark
  kar de", "ye page handle kar" → routed to the model, executed by the
  engine. The app carries the same per-comment-style complexity budget.
- **Non-vision model helper**: engine extracts a labelled accessibility tree
  + bounding boxes, so a text-only model can fill forms accurately (which
  field/where/in what order). Errors are parsed and reported back with
  selector + reason.
- **Vision model helper**: engine re-verifies every claimed click (did the
  target actually change?) so a vision model that "thinks it clicked" but
  didn't is caught and corrected.

## 11. Inbuilt dev surface

- A bundled compiler (Rust + a JS/WASM target) so "is browser ko code
  karwao" works locally; remote-access mode lets it drive the browser, run
  code, and deliver files back (same Hermes-style control loop).

## 12. Build order (milestones, each independently verifiable)

1. **M1 — Rust crate skeleton + CDP/Wire layer** (GhostEngine talks CDP, no
   CloakBrowser import). *(next)*
2. **M2 — Crypt layer**: password + cookie vault, multi-layer AEAD,
   user/admin/attacker model, tests. *(next)*
3. **M3 — Router**: same-IP enforcement + Tor/arti + proxy pool.
4. **M4 — pyo3 binding**: GhostSession rebinds to GhostEngine; kill
   CloakBrowser path.
5. **M5 — App layer**: Tauri shell, drive storage, cookie→MCP, model/AI
   helpers, GPU/low-RAM backends.

Everything is local-first; nothing connects to a server unless explicitly
told to. That is the contract.
