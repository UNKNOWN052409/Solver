# GhostMouse 🌭 — Rust agentic stealth web client (GhostRise v0.3 core)

One light Rust binary that gives AI agents a browser-shaped web client:
real-Chrome TLS (JA3/JA4/HTTP2), stable per-user identities, human-timed
behavior, tracker blocking, agentic page reads, multi-engine search,
and captcha plumbing into Solver — with zero DOM/JS engine bloat.

> Successor note: the Python GhostRise (CloakBrowser wrapper) stays for
> headed/JS-heavy targets. GhostMouse is the Rust headless-first core:
> lighter, faster, agent-native, JSONL/TCP drivable.

## Why this beats Firecrawl / TinyFish for agents

| | Firecrawl-style | GhostMouse |
|---|---|---|
| TLS fingerprint | generic reqwest/Node | real Chrome (BoringSSL, rquest-util presets 100–136) |
| Identity | ephemeral | stable per-user persona + rhythm |
| Tracker calls | many | 0 (42 domains refused locally) |
| Captcha | fails / paid API | detect → Solver API → clearance vault replay |
| Search | paid API | local SearXNG → DDG → Bing fallback chain |
| Drive API | REST crawl API | JSONL/TCP ops (MCP-bridgeable) |
| Binary | service | single ~2MB static Rust binary |
| Cost | per-page | free, local |

## Build

```bash
cargo build --release    # target/release/ghostmouse
```

BoringSSL compiles once (~20–40 min on aarch64 phone hardware, cached
afterwards). Vendored deps: `vendor/rquest` (Chrome TLS), `vendor/rquest-util`
(browser presets). No system browser needed, ever.

## Identity model

`--user alice` → FNV hash → stable persona: one of 6 Chrome 134–136
personas (Win/Mac/Linux), consistent UA + sec-ch-ua + locale + request
rhythm forever. Different users = different machines. Herd privacy:
only recent common Chrome builds, rarity is a signal.

## CLI

```bash
# readable page text
ghostmouse get https://example.com

# agent navigation map
ghostmouse links https://news.ycombinator.com

# forms with field types (login/submit plumbing)
ghostmouse form https://github.com/login

# tables, meta, images
ghostmouse table https://example.com/pricing
ghostmouse meta https://example.com
ghostmouse images https://example.com

# captcha recon: tech + sitekeys + wall status
ghostmouse sniff https://17.wtf

# X posts without login
ghostmouse x elonmusk --limit 10

# multi-engine search (SearXNG -> DDG -> Bing)
ghostmouse search "rust tls fingerprint"

# fill + submit first form (fields as name=value)
ghostmouse submit https://httpbin.org/forms/post --fields custname=lo --size=M

# solve a captcha page via the Solver API
ghostmouse solve https://target-with-hcaptcha.com \
    --api http://127.0.0.1:8000 --key sk-solver-...

# solve an image captcha file via the Solver API
ghostmouse solve-image captcha.png --api http://127.0.0.1:8000 --key ...

# which persona am I?
ghostmouse whoami
```

Global flags: `--user` (identity), `--proxy http/socks5://[user:pass@]host:port`,
`--persona win-chrome-136`, `--timeout 30`, `--no-blocklist` (debug).

## Agent drive API (JSONL/TCP)

`ghostmouse serve --bind 127.0.0.1:9410` — one JSON request per line,
one JSON response per line. Made for AI agents and MCP bridges:

```
{"op": "get",    "url": "https://example.com"}
{"op": "links",  "url": "https://example.com"}
{"op": "forms",  "url": "https://example.com"}
{"op": "text",   "url": "https://example.com"}
{"op": "meta",   "url": "https://example.com"}
{"op": "images", "url": "https://example.com"}
{"op": "tables", "url": "https://example.com"}
{"op": "search", "query": "foo", "limit": 10, "engine": "searxng"}
{"op": "x",      "handle": "elonmusk", "limit": 20}
{"op": "sniff",  "url": "https://target.com"}
{"op": "solve",  "url": "https://target.com", "api": "http://127.0.0.1:8000",
                 "api_key": "sk-solver-...", "twocaptcha_key": "..."}
{"op": "submit", "url": "https://httpbin.org/forms/post",
                 "fields": {"custname": "lo", "topping": "cheese"}}
{"op": "stats"}
```

Response: `{"ok": true, ...}` or `{"ok": false, "error": "...", "captcha": [...]}`
(wall responses carry the detected tech so the agent can route to solve).

Quick drive from Python:

```python
import socket, json
s = socket.create_connection(("127.0.0.1", 9410))
s.sendall(b'{"op": "links", "url": "https://example.com"}\n')
print(json.loads(s.recv(65536)))
```

## Captcha strategy (the anti-captcha pipeline)

1. Vault replay — `~/.solver_clearance/<domain>.json` cookies ride every
   request to that domain (Python-minted clearances work here, and
   Rust-minted ones work in Python — same schema).
2. On a wall (`is_walled`): response carries `captcha` tech list, agent
   calls `{"op": "solve"}` which hands the challenge to Solver's
   `/solve/service` (hcaptcha/recaptcha/cloudflare via 2captcha
   passthrough or your own engines).
3. Image captchas: `solve-image` uploads to Solver's `/solve/image`
   (SlotEngine 81% LCSD model or CNN/tesseract fallback).
4. Clearance cookies you capture can be saved back into the vault via
   `vault::save` (schema-identical to recon/clearance_session.py).

## Design rules (LO's)

- live-only: capabilities tested against real sites, not mocks
- one-file: whole stack in src/main.rs, no fragments
- light: no DOM engine, no JS engine — agents need clean text, links,
  forms, and a human-shaped wire profile, not rendering
