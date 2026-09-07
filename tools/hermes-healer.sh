#!/usr/bin/env bash
# hermes-healer — Hermes/session ko kill hone se roke + khud fix kare.
#
# Har 2-min cron. Logic:
#   1. HERMES CORE: agar hermes main agent/worker procs < MIN, to uske
#      @reboot/session-start command se restart karo (append to log).
#   2. KEEPALIVE: screen-off survival layer agar mara to revive.
#   3. SELF-FIX: stale lockfiles /tmp/.X99-lock, /tmp/.hermes-* clean.
#      (ye root causes the the: Xvfb down -> browser crashes -> session
#      kill -> keepalive skeletal.)
#
# Never tries to restart what's already healthy (idempotent).

LOG=/tmp/opencode/hermes-healer.log
HERMES_MIN=2            # kam se kam zinda hermes procs
mkdir -p "$(dirname "$LOG")"
ts() { echo "[$(date '+%m-%d %H:%M:%S')] $*"; }

# ---- 0. SIGTERM/SIGHUP self-guard: kisi process pattern ko kabhi kill na kare
#      (ya to hermes ka apna procguard hi hai — yahan sirf RESTART karna hai)

# ---- 1. HERMES CORE alive check
n=$(pgrep -f "hermes" | wc -l)
if [ "$n" -lt "$HERMES_MIN" ]; then
    ts "WARN hermes procs=$n (< $HERMES_MIN) — restart attempt" >> "$LOG"
    # hermes session start command (profile-based)
    if [ -x "/home/kali/hermes" ]; then
        nohup /home/kali/hermes >/dev/null 2>&1 &
        ts "  restarted /home/kali/hermes (pid $!)" >> "$LOG"
    elif command -v hermes >/dev/null 2>&1; then
        setsid nohup hermes >/dev/null 2>&1 &
        ts "  restarted hermes (pid $!)" >> "$LOG"
    else
        ts "  no hermes binary found — manual attention" >> "$LOG"
    fi
    sleep 10
    n2=$(pgrep -f "hermes" | wc -l)
    ts "  post-restart procs=$n2" >> "$LOG"
fi

# ---- 2. KEEPALIVE revive (screen-off survival + phantom-killer shield)
if ! pgrep -f "keepalive.sh start|while :; do.*touch /tmp/opencode/keepalive.heartbeat" >/dev/null 2>&1; then
    ts "keepalive DOWN -> revive" >> "$LOG"
    nohup bash /home/kali/NeoSolver/tools/keepalive.sh start >/dev/null 2>&1 &
    ts "  keepalive restarted (pid $!)" >> "$LOG"
fi

# ---- 3. SELF-FIX stale locks (root causes)
for lock in /tmp/.X99-lock /tmp/.X11-unix/X99 /tmp/.hermes-*.lock; do
    if [ -e "$lock" ]; then
        ts "stale lock $lock -> remove" >> "$LOG"
        rm -f "$lock" 2>/dev/null
    fi
done
# Xvfb down but lock present -> restart
if ! xdpyinfo -display :99 >/dev/null 2>&1; then
    rm -f /tmp/.X99-lock 2>/dev/null
    bash /home/kali/NeoSolver/tools/xvfb-start.sh start >/dev/null 2>&1
    ts "xvfb :99 revived" >> "$LOG"
fi

# ---- 4. zombie chrome sweep (10+ procs = leak, browser crashes ko boost)
#      sirf /tmp/gw* wale chrome zombies — hermes ke apne kabhi nahi
zb=$(pgrep -f "user-data-dir=/tmp/gw" | wc -l)
if [ "$zb" -gt 12 ]; then
    ts "zombie chrome count=$zb ($zb>12) -> sweep" >> "$LOG"
    for p in $(pgrep -f "user-data-dir=/tmp/gw"); do
        kill -9 "$p" 2>/dev/null
    done
    ts "  swept $zb chrome zombies" >> "$LOG"
fi

# ---- 5. qwen-bridge revive (uni endpoint — hermes alias /model qwen)
if ! pgrep -f "qwen_bridge.py|qwen_browser_bridge.py" >/dev/null 2>&1; then
    ts "qwen-bridge DOWN -> revive" >> "$LOG"
    nohup /home/kali/Rev/venv/bin/python /home/kali/Rev/qwen_browser_bridge.py --serve --headless >/tmp/qwen_serve.log 2>&1 &
    ts "  qwen-bridge restarted (pid $!)" >> "$LOG"
fi

ts "ok (hermes=$n)" >> "$LOG"
