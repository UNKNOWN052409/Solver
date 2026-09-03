#!/bin/bash
# Watchdog: keeps IPGate (8899) and GhostPipe (8900) alive 24/7.
# Designed for cron: runs every minute, revives anything dead,
# survives reboots via @reboot entry. No process-pattern kills -
# only port health checks decide, so it can never self-terminate.

SOLVER=/home/kali/Solver
LOG=/tmp/opencode/watchdog.log
log() { echo "[$(date '+%m-%d %H:%M:%S')] $*" >> "$LOG"; }

alive() { curl -s --max-time 5 "$1" > /dev/null 2>&1; }

# ---- IPGate :8899 ----
if ! alive http://127.0.0.1:8899/status; then
    if [ -f /tmp/opencode/ipgate.pid ]; then
        kill -9 "$(cat /tmp/opencode/ipgate.pid)" 2>/dev/null
        rm -f /tmp/opencode/ipgate.pid
    fi
    log "ipgate DOWN -> reviving"
    cd "$SOLVER" || exit 1
    nohup python3 recon/ipgate.py --port 8899 >> /tmp/opencode/ipgate_out.log 2>&1 &
    echo $! > /tmp/opencode/ipgate.pid
    log "ipgate revived (pid $(cat /tmp/opencode/ipgate.pid))"
else
    # refresh pidfile if missing (adopt running instance)
    if [ ! -f /tmp/opencode/ipgate.pid ]; then
        pgrep -f "ipgate.py --port 8899" | head -1 > /tmp/opencode/ipgate.pid 2>/dev/null
    fi
fi

# ---- GhostPipe :8900 ----
if ! alive http://127.0.0.1:8900/stats; then
    if [ -f /tmp/opencode/ghostpipe.pid ]; then
        kill -9 "$(cat /tmp/opencode/ghostpipe.pid)" 2>/dev/null
        rm -f /tmp/opencode/ghostpipe.pid
    fi
    log "ghostpipe DOWN -> reviving"
    cd "$SOLVER" || exit 1
    nohup python3 recon/ghostpipe.py --port 8900 >> /tmp/opencode/gp.log 2>&1 &
    echo $! > /tmp/opencode/ghostpipe.pid
    log "ghostpipe revived (pid $(cat /tmp/opencode/ghostpipe.pid))"
fi

# ---- torbugbot : Telegram adaptive filter bot (02-Sep) ----
# Liveness: bot.py khud apna PID bot.pid me likhta hai. Check = PID zinda + cmdline bot.py.
TB_PIDFILE=/home/kali/Filtering/bot.pid
TB_ALIVE=0
if [ -f "$TB_PIDFILE" ]; then
    TB_PID=$(cat "$TB_PIDFILE" 2>/dev/null)
    if [ -n "$TB_PID" ] && [ -d "/proc/$TB_PID" ]; then
        TB_CMD=$(tr '\0' ' ' < "/proc/$TB_PID/cmdline" 2>/dev/null)
        case "$TB_CMD" in *bot.py*) TB_ALIVE=1;; esac
    fi
fi
if [ "$TB_ALIVE" -eq 0 ]; then
    log "torbugbot DOWN -> reviving"
    ( cd /home/kali/Filtering
      BOT_TOKEN="$(cat /home/kali/Filtering/bot.token)" \
      nohup python3 bot.py >> /home/kali/Filtering/bot.log 2>&1 & )
    sleep 5
    for _i in 1 2 3; do
        if [ -f "$TB_PIDFILE" ] && [ -d "/proc/$(cat $TB_PIDFILE 2>/dev/null)" ]; then
            log "torbugbot revive OK (pid $(cat $TB_PIDFILE))"
            exit_torbug=1
            break
        fi
        sleep 2
    done
    if [ "${exit_torbug:-0}" -ne 1 ]; then
        log "torbugbot revive FAILED — check /home/kali/Filtering/bot.log"
    fi
fi
