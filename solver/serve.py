"""Solver API launcher with automatic port failover and 24/7 support.

Problem it solves (live-verified on Kali-on-Android/proot):
  - port 8000 can get phantom-locked by the Android TCP table while no
    process is listening — uvicorn binds, everything else sees EADDRINUSE
    forever until reboot. Same can happen to any fixed port.

What it does:
  - tries PREFERRED_PORTS in order (default 8010, 8000, 8011..8019)
  - skips a port if something healthy already answers on it (ours? reuse.
    not ours? move on)
  - binds the first healthy port, publishes it to a discovery file so
    clients/watchdogs always find the live API without guessing
  - single instance lock via the discovery file's pid line

Usage:
  python -m solver.serve                     # daemon mode (nohup-friendly)
  python -m solver.serve --fg                # foreground (debug)
  python -m solver.serve --port 9000         # preferred first port
  cat ~/.solver_api_port                     # where is it running now?
  curl http://127.0.0.1$(<~/.solver_api_port)/health

Watchdog integration (hermes-watchdog.sh calls this every minute):
  it just re-runs `solver.serve` — if the API is alive, this exits
  instantly (lock check); if dead, it revives on the first free port.
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

DISCOVERY = Path(os.environ.get("SOLVER_DISCOVERY", "~/.solver_api_port")).expanduser()
PREFERRED_PORTS = [8010, 8000, 8011, 8012, 8013, 8014, 8015, 8016, 8017, 8018, 8019]
HEALTH_PATH = "/health"
HEALTH_TIMEOUT = 3.0
PYTHON = sys.executable
ROOT = Path(__file__).resolve().parent.parent


def _log(msg: str) -> None:
    print(f"[solver.serve] {msg}", flush=True)


def _api_healthy(port: int) -> bool:
    """True if some HTTP server answers /health on this port."""
    try:
        import urllib.request
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{HEALTH_PATH}", timeout=HEALTH_TIMEOUT
        ) as r:
            return r.status == 200
    except Exception:
        return False


def _port_bindable(port: int) -> bool:
    """True if we can bind now (phantom-locked ports fail here too)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            return True
    except OSError:
        return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_discovery() -> dict:
    try:
        return json.loads(DISCOVERY.read_text())
    except Exception:
        return {}


def _write_discovery(port: int, pid: int) -> None:
    DISCOVERY.write_text(json.dumps({
        "port": port, "pid": pid, "url": f"http://127.0.0.1:{port}",
        "started": time.time(),
    }))
    _log(f"discovery: {DISCOVERY} -> port {port} (pid {pid})")


def _own_instance(port: int) -> bool:
    """True when the API answering on this port is a solver.server we
    launched (shared venv/host — probe a solver-specific endpoint)."""
    try:
        import urllib.request
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=HEALTH_TIMEOUT
        ) as r:
            body = r.read().decode()[:400]
            return '"ok"' in body and ("engines" in body or "slot" in body)
    except Exception:
        return False


def acquire_port(preferred: int | None) -> int | None:
    order = ([preferred] if preferred else []) + [
        p for p in PREFERRED_PORTS if p != preferred
    ]
    for port in order:
        if _api_healthy(port) and _own_instance(port):
            _log(f"port {port}: healthy solver API already running — reusing")
            return port
        if _port_bindable(port):
            _log(f"port {port}: free — binding here")
            return port
        _log(f"port {port}: busy/locked — skipping")
    return None


def already_running() -> int | None:
    d = _read_discovery()
    pid, port = d.get("pid"), d.get("port")
    if isinstance(pid, int) and isinstance(port, int) and _pid_alive(pid) and _api_healthy(port):
        _log(f"already running on port {port} (pid {pid}) — nothing to do")
        return port
    return None


def spawn(preferred: int | None, fg: bool) -> int:
    port = acquire_port(preferred)
    if port is None:
        _log("no free port in scan range 8000-8019 — giving up")
        sys.exit(1)
    env = dict(os.environ)
    env.setdefault("SOLVER_MODEL_DIR", "/home/kali/data")
    cmd = [PYTHON, "-m", "uvicorn", "solver.server:app", "--host", "127.0.0.1",
           "--port", str(port)]
    if fg:
        _write_discovery(port, os.getpid())
        env["PYTHONPATH"] = str(ROOT)
        os.execvpe(cmd[0], cmd, env)  # replace self — foreground uvicorn
    # daemon mode: uvicorn in background, we become the supervisor
    child = subprocess.Popen(cmd, cwd=str(ROOT), env=env,
                             stdout=open("/tmp/solver_api.log", "a"),
                             stderr=subprocess.STDOUT)
    _write_discovery(port, child.pid)
    _log(f"uvicorn spawned (pid {child.pid}) on port {port} — supervising")

    # Supervisor loop: Android phantom-killer and OOM revive.
    # `warmed` = API answered /health at least once. Hangs are only
    # actionable AFTER warming (uvicorn needs ~5-8s to start; checking
    # earlier false-kills healthy spawns — live-verified bug).
    # Hard limit: a spawn that never warms in 90s is also replaced.
    STARTUP_LIMIT = 90.0

    def respawn(reason: str):
        nonlocal port, child, warmed, spawn_time
        _log(f"{reason} — respawning")
        try:
            child.kill()
        except Exception:
            pass
        time.sleep(3)
        port = acquire_port(preferred) or port
        child = subprocess.Popen(cmd, cwd=str(ROOT), env=env,
                                 stdout=open("/tmp/solver_api.log", "a"),
                                 stderr=subprocess.STDOUT)
        _write_discovery(port, child.pid)
        warmed = False
        spawn_time = time.time()

    warmed = False
    spawn_time = time.time()
    while True:
        time.sleep(30)
        rc = child.poll()
        if rc is not None:
            respawn(f"uvicorn exited rc={rc}")
            continue
        if _api_healthy(port):
            warmed = True
            continue
        if warmed:
            respawn("uvicorn hung (was healthy, port dead)")
        elif time.time() - spawn_time > STARTUP_LIMIT:
            respawn("uvicorn never became healthy in 90s")


def main() -> None:
    ap = argparse.ArgumentParser(description="Solver API launcher (auto port + 24/7)")
    ap.add_argument("--port", type=int, help="preferred first port")
    ap.add_argument("--fg", action="store_true", help="foreground mode (no supervisor)")
    args = ap.parse_args()

    # single-instance: healthy API running? exit 0 — watchdog-friendly
    if already_running() is not None:
        sys.exit(0)
    # stale discovery (dead pid): fall through and rebind
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    spawn(args.port, args.fg)


if __name__ == "__main__":
    main()
