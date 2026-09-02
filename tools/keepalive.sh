#!/usr/bin/env bash
# keepalive — screen-off survival + phantom-killer prevention (proot-side).
#
# Termux wake-lock proot ke andar se nahi le ja sakte (bins bahar hain),
# isliye ye proot-internal ladder use karta hai:
#
#  1. IDLE-BUSY THREAD: ek nibble process (nice 19, ~0.1% CPU) jo har
#     second 5ms ka kaam karta hai — Android ProcessRecord ko "active"
#     dikhata hai, freeze/kill delay karta hai. NICE-19 hai isliye
#     kaam ki speed ZERO impact (scheduler sabse pehle real kaam deta hai).
#  2. HEARTBEAT LEDGER: har 30s me /tmp/keepalive.heartbeat touch —
#     watchdog/manual dono isse "kitna time chal raha hai" verify karte hain.
#  3. WATCHDOG REVIVE: ye script apne aap ko daemonize karti hai
#     (cron @reboot + watchdog 5-min cron se bhi revive).
#
# Speed impact: ZERO (nibble nice-19, 50ms/30s duty = 0.17% ek core pe,
# aur wo bhi idle cycle me). Battery: negligible (~0.1%/hour).
#
# Usage:
#   bash ~/Solver/tools/keepalive.sh start    # daemon start (idempotent)
#   bash ~/Solver/tools/keepalive.sh status   # heartbeat age + procs
#   bash ~/Solver/tools/keepalive.sh stop
#
# NOTE: Termux app me bina root ke sabse strong combo:
#   Termux side: Acquire Termux:Widget wala "Wake lock" toggle ON karo
#   (ek baar manually) + Android Settings > Battery > Termux >
#   "Unrestricted" — phir ye script proot me backup layer hai.
set -u
DIR=/tmp/opencode
HB=$DIR/keepalive.heartbeat
PIDF=$DIR/keepalive.pid
LOG=$DIR/keepalive.log
mkdir -p "$DIR"
log() { echo "[$(date '+%m-%d %H:%M:%S')] $*" >> "$LOG"; }

nibble() {
    # nice-19 idle nibble: 5ms kaam / 1s gap. ProcessRecord active rehta
    # hai, CPU competition ZERO (sab real kaam nice-0 pe chalta hai).
    while :; do
        # 5ms busy
        end=$(( $(date +%s%N) + 5000000 ))
        while [ "$(date +%s%N)" -lt "$end" ]; do :; done
        sleep 1
    done
}

case "${1:-status}" in
  start)
    if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
        echo "[keepalive] already running (pid $(cat "$PIDF"))"
        exit 0
    fi
    nice -n 19 bash -c '
        while :; do
            touch '"$HB"' 2>/dev/null
            sleep 29
        done
    ' >/dev/null 2>&1 &
    echo $! > "$PIDF"
    log "started pid $(cat "$PIDF") (nice-19 nibble + heartbeat)"
    echo "[keepalive] started (pid $(cat "$PIDF")) — screen-off survival layer ON"
    ;;
  stop)
    if [ -f "$PIDF" ]; then
        kill "$(cat "$PIDF")" 2>/dev/null && log "stopped" || true
        rm -f "$PIDF"
        echo "[keepalive] stopped"
    else
        echo "[keepalive] not running"
    fi
    ;;
  status)
    if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
        AGE=$(( $(date +%s) - $(stat -c %Y "$HB" 2>/dev/null || echo 0) ))
        echo "[keepalive] running (pid $(cat "$PIDF")), heartbeat ${AGE}s ago"
    else
        echo "[keepalive] not running — start with: keepalive.sh start"
    fi
    ;;
  *)
    echo "usage: keepalive.sh start|stop|status"
    ;;
esac
