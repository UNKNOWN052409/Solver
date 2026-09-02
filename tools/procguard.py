#!/usr/bin/env python3
"""procguard — Termux 32-child cap ka pehra.

Problem (live-diagnosed Sep 2026): Kali-on-Android/proot me Termux ka
32-child process cap hai. Heavy test runs (CloakBrowser/playwright
har launch pe 8+ children spawn karte hain) cap bhar dete hain ->
Android phantom-killer DUSRE sessions ko maar deta hai (parallel DK
Solver session worker mara tha).

Ye tool test-ke-baad cleanup karta hai — sirf TEST procs ko, watchdog
daemons (burp-mcp, searxng, prexzy, hermes) ko kabhi nahi.

Usage:
    python3 ~/Solver/tools/procguard.py            # dry-run report
    python3 ~/Solver/tools/procguard.py --kill     # cleanup
    # test scripts me (hamesha finally block me):
    #   subprocess.run(["python3", ".../procguard.py", "--kill"])
"""
import argparse
import os
import signal
import time

# in proc ko KABHI nahi maarte — watchdog/infra
PROTECTED = (
    "burp-mcp", "server.py:9876",       # burp MCP
    "searx.webapp",                     # SearXNG
    "prexzy-proxy", "prexzy-sse",      # Prexzy
    "hermes",                           # hermes agents/workers/kernels
    "watchdog",
    "crond", "cron",
    "kanban",
    "procguard",                        # khud
)

# test-lifecycle procs — inko cleanup karte hain
TEST_PATTERNS = (
    "ghostmouse serve",
    "cloakbrowser", "geckodriver", "firefox", "chromium",
    "playwright",                       # playwright driver binaries
    "uvicorn",                          # test servers (mock upstreams etc)
    "revd",                             # Rust engine test instance
    "solver.server", "solver.vision.serve",
    "mitmdump",                         # test captures
    "mock_articles", "mock_qwen",
)

HERMES_DIR = "/home/kali/.hermes"


def _cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().decode(errors="replace").replace("\0", " ").strip()
    except Exception:
        return ""


def _alive_ppid_is_hermes(pid):
    """Hermes apne kernels/tool-calls ke liye py spawn karta hai — usko
    protected treat karo (cmdline me hermes path hota hai)."""
    cmd = _cmdline(pid)
    return HERMES_DIR in cmd


def find_test_procs():
    hits = []
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        pid = int(p)
        cmd = _cmdline(pid)
        if not cmd:
            continue
        if _alive_ppid_is_hermes(pid):
            continue
        if any(k in cmd for k in PROTECTED):
            continue
        if any(k in cmd for k in TEST_PATTERNS):
            hits.append((pid, cmd[:80]))
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kill", action="store_true", help="actually kill (default: dry-run)")
    args = ap.parse_args()

    hits = find_test_procs()
    if not hits:
        print("[procguard] clean — koi test-proc zombie nahi")
        return

    verb = "KILL" if args.kill else "DRY-RUN"
    print(f"[procguard] {verb}: {len(hits)} test procs")
    for pid, cmd in hits:
        print(f"  {pid:6d} {cmd}")
        if args.kill:
            try:
                os.kill(pid, signal.SIGKILL)
                print(f"         killed")
            except ProcessLookupError:
                print(f"         already gone")
            except PermissionError:
                print(f"         no permission (skip)")
    if args.kill:
        time.sleep(0.5)
        left = find_test_procs()
        print(f"[procguard] after cleanup: {len(left)} remaining")
        if left:
            for pid, cmd in left:
                print(f"  STILL: {pid} {cmd[:60]}")


if __name__ == "__main__":
    main()
