#!/usr/bin/env python3
"""capwatch — session-budget guard for Kali-on-Android (proot).

3-4 parallel Hermes sessions chalane ke liye:
  1. BUDGET report: 32-child Termux cap me kitna headroom hai
  2. Browser-kill: heavy browsers (8+ children each) cleanup —
     sessions ko bachata hai
  3. Session-safe: hermes/daemons kabhi touch nahi

Power-efficient design:
  - no polling loops, no background daemon — run karo, jawab do, exit
  - cron/watchdog me bas 1 line: capwatch --auto (5 min interval pe)
  - cleanup sirf tab jab headroom <8 ho (browsers pehle kill, warna skip)

Usage:
    python3 ~/Solver/tools/capwatch.py            # status report
    python3 ~/Solver/tools/capwatch.py --auto    # smart cleanup (idempotent)
    python3 ~/Solver/tools/capwatch.py --browsers # force browser cleanup
"""
import os
import signal
import sys
import time

CAP = 32                    # Termux child cap
MIN_HEADROOM = 8            # isse kam -> browsers clean karo
BROWSER_HEAVY = 3            # browsers ko HEADROOM-BUDGET me count karo

PROTECTED_MARKERS = (
    "hermes", "hermes-agent",       # saare sessions + kernels + workers
    "burp-mcp", "server.py:9876",
    "searx.webapp",
    "prexzy",
    "watchdog", "crond", "cron", "kanban",
    "capwatch", "procguard",
    "sshd", "dropbear", "termux",
)

BROWSER_MARKERS = (
    "geckodriver", "firefox", "chromium", "chrome",
    "cloakbrowser", "playwright", "headless_shell",
    "mock_articles", "mock_qwen", "ghostmouse serve",
    "uvicorn", "revd", "mitmdump",
)


def _cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().decode(errors="replace").replace("\0", " ").strip()
    except Exception:
        return ""


def _is_hermes_path(pid):
    cmd = _cmdline(pid)
    return "/.hermes/" in cmd


def scan():
    """Return (total, browsers, daemons, sessions)."""
    total, browsers, daemons, sessions = 0, [], [], 0
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        cmd = _cmdline(p)
        if not cmd:
            continue
        total += 1
        if any(m in cmd for m in PROTECTED_MARKERS) or _is_hermes_path(int(p)):
            if "hermes" in cmd:
                sessions += 1
            continue
        if any(m in cmd for m in BROWSER_MARKERS):
            browsers.append((int(p), cmd[:70]))
    return total, browsers, sessions


def kill_browsers(browsers):
    for pid, cmd in browsers:
        try:
            os.kill(pid, signal.SIGKILL)
            print(f"  killed {pid}: {cmd}")
        except Exception as e:
            print(f"  skip {pid}: {e}")


def main():
    args = sys.argv[1:]
    total, browsers, sessions = scan()
    headroom = CAP - total

    print(f"[capwatch] procs {total}/{CAP} | headroom {headroom} "
          f"| hermes-sessions {sessions} | browsers {len(browsers)}")

    if "--auto" in args:
        # power-efficient: sirf tab cleanup jab zaroorat ho
        if headroom < MIN_HEADROOM and browsers:
            print(f"[capwatch] headroom {headroom} < {MIN_HEADROOM} — "
                  f"browser cleanup ({len(browsers)})")
            kill_browsers(browsers)
            time.sleep(0.5)
            t2, b2, s2 = scan()
            print(f"[capwatch] after: {t2}/{CAP} | headroom {CAP - t2} | browsers {len(b2)}")
        else:
            print("[capwatch] healthy — no action")
    elif "--browsers" in args:
        if browsers:
            kill_browsers(browsers)
        else:
            print("[capwatch] no browsers running")
    # plain run = report only


if __name__ == "__main__":
    main()
